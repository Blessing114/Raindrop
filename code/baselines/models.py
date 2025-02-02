import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import os
os.add_dll_directory('c:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v10.1/bin')
os.add_dll_directory(os.path.dirname(__file__))

from torch.nn.parameter import Parameter
from transformer_conv import TransformerConv


from torch_geometric.nn import GINConv, global_add_pool
from torch.nn import Sequential, Linear, BatchNorm1d, ReLU

import warnings
import numbers
from torch.nn.init import xavier_uniform_


class PositionalEncodingTF(nn.Module):
    def __init__(self, d_model, max_len=500, MAX=10000,):
        super(PositionalEncodingTF, self).__init__()
        self.max_len = max_len
        self.d_model = d_model
        self.MAX = MAX
        self._num_timescales = d_model // 2

    def getPE(self, P_time):
        B = P_time.shape[1]

        P_time = P_time.float()

        timescales = self.max_len ** np.linspace(0, 1, self._num_timescales)

        times = torch.Tensor(P_time.cpu()).unsqueeze(2)

        scaled_time = times / torch.Tensor(timescales[None, None, :])
        # Use a 32-D embedding to represent a single time point
        pe = torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], axis=-1)  # T x B x d_model
        pe = pe.type(torch.FloatTensor)

        return pe

    def forward(self, P_time):
        pe = self.getPE(P_time)
        pe = pe.cuda()
        return pe


class TransformerModel(nn.Module):
    """ Transformer model with context embedding, aggregation
    Inputs:
        d_inp = number of input features
        d_model = number of expected model input features
        nhead = number of heads in multihead-attention
        nhid = dimension of feedforward network model
        dropout = dropout rate (default 0.1)
        max_len = maximum sequence length
        MAX  = positional encoder MAX parameter
        n_classes = number of classes
    """

    def __init__(self, d_inp, d_model, nhead, nhid, nlayers, dropout, max_len, d_static, MAX, n_classes):
        super(TransformerModel, self).__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'

        #         self.pos_encoder = PositionalEncoding0(d_model, max_len, MAX)
        self.pos_encoder = PositionalEncodingTF(d_model, max_len, MAX)

        encoder_layers = TransformerEncoderLayer(d_model, nhead, nhid, dropout)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)

        self.encoder = nn.Linear(d_inp, d_model)
        self.d_model = d_model

        self.emb = nn.Linear(d_static, d_model)

        self.mlp = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(),
            nn.Linear(d_model, n_classes),
        )

        self.relu = nn.ReLU()

        self.init_weights()

    def init_weights(self):
        initrange = 1e-10
        self.encoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, src, static, times, lengths):
        maxlen, batch_size = src.shape[0], src.shape[1]

        src = self.encoder(src) * math.sqrt(self.d_model)

        pe = self.pos_encoder(times)

        src = src + pe

        if static is not None:
            emb = self.emb(static)

        # append context on front
        x = torch.cat([emb.unsqueeze(0), src], dim=0)

        mask = torch.arange(maxlen + 1)[None, :] >= (lengths.cpu()[:, None] + 1)
        mask = mask.squeeze(1).cuda()
        output = self.transformer_encoder(x, src_key_padding_mask=mask)

        # masked aggregation
        mask2 = mask.permute(1, 0).unsqueeze(2).long()
        lengths2 = lengths.unsqueeze(1)
        output = torch.sum(output * (1 - mask2), dim=0) / (lengths2 + 1)

        # feed through MLP
        output = self.mlp(output)
        return output


class TransformerModel2(nn.Module):
    """ Transformer model with context embedding, aggregation, split dimension positional and element embedding
    Inputs:
        d_inp = number of input features
        d_model = number of expected model input features
        nhead = number of heads in multihead-attention
        nhid = dimension of feedforward network model
        dropout = dropout rate (default 0.1)
        max_len = maximum sequence length
        MAX  = positional encoder MAX parameter
        n_classes = number of classes
    """

    def __init__(self, d_inp, d_model, nhead, nhid, nlayers, dropout, max_len, d_static, MAX, perc, aggreg, n_classes, static=True):
        super(TransformerModel2, self).__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'

        d_pe = 16
        d_enc = d_inp

        self.pos_encoder = PositionalEncodingTF(d_pe, max_len, MAX)

        encoder_layers = TransformerEncoderLayer(d_pe+d_enc, nhead, nhid, dropout)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)

        self.encoder = nn.Linear(d_inp, d_enc)

        self.static = static
        if self.static:
            self.emb = nn.Linear(d_static, d_inp)

        if static == False:
            d_fi = d_enc + d_pe
        else:
            d_fi = d_enc + d_pe + d_inp

        self.mlp = nn.Sequential(
            nn.Linear(d_fi, d_fi),
            nn.ReLU(),
            nn.Linear(d_fi, n_classes),
        )

        self.aggreg = aggreg

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.init_weights()

    def init_weights(self):
        initrange = 1e-10
        self.encoder.weight.data.uniform_(-initrange, initrange)
        if self.static:
            self.emb.weight.data.uniform_(-initrange, initrange)

    def forward(self, src, static, times, lengths):
        maxlen, batch_size = src.shape[0], src.shape[1]

        src = src[:, :, :int(src.shape[2] / 2)]

        src = self.encoder(src)

        pe = self.pos_encoder(times)
        src = torch.cat([pe, src], axis=2)

        src = self.dropout(src)

        if static is not None:
            emb = self.emb(static)

        x = src

        # mask out the all-zero rows
        mask = torch.arange(maxlen)[None, :] >= (lengths.cpu()[:, None])
        mask = mask.squeeze(1).cuda()

        output = self.transformer_encoder(x, src_key_padding_mask=mask)

        mask2 = mask.permute(1, 0).unsqueeze(2).long()
        if self.aggreg == 'mean':
            lengths2 = lengths.unsqueeze(1)
            output = torch.sum(output * (1 - mask2), dim=0) / (lengths2 + 1)
        elif self.aggreg == 'max':
            output, _ = torch.max(output * ((mask2 == 0) * 1.0 + (mask2 == 1) * -10.0), dim=0)

        # feed through MLP
        if static is not None:
            output = torch.cat([output, emb], dim=1)
        output = self.mlp(output)
        return output


class SEFT(nn.Module):
    """ Transformer model with context embedding, aggregation, split dimension positional and element embedding
    Inputs:
        d_inp = number of input features
        d_model = number of expected model input features
        nhead = number of heads in multihead-attention
        nhid = dimension of feedforward network model
        dropout = dropout rate (default 0.1)
        max_len = maximum sequence length
        MAX  = positional encoder MAX parameter
        n_classes = number of classes
    """

    def __init__(self, d_inp, d_model, nhead, nhid, nlayers, dropout, max_len, d_static, MAX, perc, aggreg, n_classes,
                 static=True):
        super().__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'

        d_pe = 16
        d_enc = d_inp

        self.pos_encoder = PositionalEncodingTF(d_pe, max_len, MAX)
        self.pos_encoder_value = PositionalEncodingTF(d_pe, max_len, MAX)
        self.pos_encoder_sensor = PositionalEncodingTF(d_pe, max_len, MAX)

        self.linear_value = nn.Linear(1, 16)
        self.linear_sensor = nn.Linear(1, 16)

        self.d_K = 2 * (d_pe+ 16+16)

        encoder_layers = TransformerEncoderLayer(self.d_K, 1, nhid, dropout)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)

        encoder_layers_f_prime = TransformerEncoderLayer(int(self.d_K//2), 1, nhid, dropout)
        self.transformer_encoder_f_prime = TransformerEncoder(encoder_layers_f_prime, 2)

        self.emb = nn.Linear(d_static, 16)

        self.proj_weight = Parameter(torch.Tensor(self.d_K, 128))

        self.lin_map = nn.Linear(self.d_K, 128)

        d_fi = 128 + 16

        if static == False:
            d_fi = 128
        else:
            d_fi = 128 + d_pe
        self.mlp = nn.Sequential(
            nn.Linear(d_fi, d_fi),
            nn.ReLU(),
            nn.Linear(d_fi, n_classes),
        )

        self.aggreg = aggreg

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.init_weights()

    def init_weights(self):
        initrange = 1e-10
        self.emb.weight.data.uniform_(-initrange, initrange)
        self.linear_value.weight.data.uniform_(-initrange, initrange)
        self.linear_sensor.weight.data.uniform_(-initrange, initrange)
        self.lin_map.weight.data.uniform_(-initrange, initrange)
        xavier_uniform_(self.proj_weight)

    def forward(self, src, static, times, lengths):
        maxlen, batch_size = src.shape[0], src.shape[1]

        src = src.permute(1, 0,2)
        fea = src[:, :, :int(src.shape[2]/2)]

        output = torch.zeros((batch_size, self.d_K)).cuda()
        for i in range(batch_size):
            nonzero_index = fea[i].nonzero(as_tuple=False)
            if nonzero_index.shape[0] == 0:
                continue
            values = fea[i][nonzero_index[:,0], nonzero_index[:,1]] # v in SEFT paper
            time_index = nonzero_index[:, 0]
            time_sequence = times[:, i]
            time_points = time_sequence[time_index]  # t in SEFT paper
            pe_ = self.pos_encoder(time_points.unsqueeze(1)).squeeze(1)

            variable = nonzero_index[:, 1]  # the dimensions of variables. The m value in SEFT paper.

            unit = torch.cat([pe_, values.unsqueeze(1), variable.unsqueeze(1)], dim=1)

            variable_ = self.pos_encoder_sensor(variable.unsqueeze(1)).squeeze(1)

            values_ = self.linear_value(values.float().unsqueeze(1)).squeeze(1)

            unit = torch.cat([pe_, values_, variable_], dim=1)

            f_prime = torch.mean(unit, dim=0)

            x = torch.cat([f_prime.repeat(unit.shape[0], 1), unit], dim=1)

            x = x.unsqueeze(1)

            output_unit = x
            output_unit = torch.mean(output_unit, dim=0)
            output[i, :] = output_unit

        output = self.lin_map(output)

        if static is not None:
            emb = self.emb(static)

        # feed through MLP
        if static is not None:
            output = torch.cat([output, emb], dim=1)
        output = self.mlp(output)
        return output


class GRUD(torch.nn.Module):
    def __init__(self, input_size, hidden_size, output_size, num_layers=1, x_mean=0,
                 bias=True, batch_first=False, bidirectional=False, dropout_type='mloss', dropout=0.0, static=True):
        super(GRUD, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.num_layers = num_layers
        self.zeros = torch.autograd.Variable(torch.zeros(input_size))
        self.x_mean = x_mean.clone().detach().requires_grad_(True)
        self.bias = bias
        self.batch_first = batch_first
        self.dropout_type = dropout_type
        self.dropout = dropout
        self.bidirectional = bidirectional
        num_directions = 2 if bidirectional else 1

        if not isinstance(dropout, numbers.Number) or not 0 <= dropout <= 1 or \
                isinstance(dropout, bool):
            raise ValueError("dropout should be a number in range [0, 1] "
                             "representing the probability of an element being "
                             "zeroed")
        if dropout > 0 and num_layers == 1:
            warnings.warn("dropout option adds dropout after all but last "
                          "recurrent layer, so non-zero dropout expects "
                          "num_layers greater than 1, but got dropout={} and "
                          "num_layers={}".format(dropout, num_layers))

        self._all_weights = []

        # decay rates gamma
        w_dg_x = torch.nn.Parameter(torch.Tensor(input_size))
        w_dg_h = torch.nn.Parameter(torch.Tensor(hidden_size))

        # z
        w_xz = torch.nn.Parameter(torch.Tensor(input_size))
        w_hz = torch.nn.Parameter(torch.Tensor(hidden_size))
        w_mz = torch.nn.Parameter(torch.Tensor(input_size))

        # r
        w_xr = torch.nn.Parameter(torch.Tensor(input_size))
        w_hr = torch.nn.Parameter(torch.Tensor(hidden_size))
        w_mr = torch.nn.Parameter(torch.Tensor(input_size))

        # h_tilde
        w_xh = torch.nn.Parameter(torch.Tensor(input_size))
        w_hh = torch.nn.Parameter(torch.Tensor(hidden_size))
        w_mh = torch.nn.Parameter(torch.Tensor(input_size))

        # y (output)
        w_hy = torch.nn.Parameter(torch.Tensor(output_size, hidden_size))

        # bias
        b_dg_x = torch.nn.Parameter(torch.Tensor(hidden_size))
        b_dg_h = torch.nn.Parameter(torch.Tensor(hidden_size))
        b_z = torch.nn.Parameter(torch.Tensor(hidden_size))
        b_r = torch.nn.Parameter(torch.Tensor(hidden_size))
        b_h = torch.nn.Parameter(torch.Tensor(hidden_size))
        b_y = torch.nn.Parameter(torch.Tensor(output_size))

        layer_params = (w_dg_x, w_dg_h,
                        w_xz, w_hz, w_mz,
                        w_xr, w_hr, w_mr,
                        w_xh, w_hh, w_mh,
                        w_hy,
                        b_dg_x, b_dg_h, b_z, b_r, b_h, b_y)

        param_names = ['weight_dg_x', 'weight_dg_h',
                       'weight_xz', 'weight_hz', 'weight_mz',
                       'weight_xr', 'weight_hr', 'weight_mr',
                       'weight_xh', 'weight_hh', 'weight_mh',
                       'weight_hy']
        if bias:
            param_names += ['bias_dg_x', 'bias_dg_h',
                            'bias_z',
                            'bias_r',
                            'bias_h',
                            'bias_y']

        for name, param in zip(param_names, layer_params):
            setattr(self, name, param)
        self._all_weights.append(param_names)

        self.flatten_parameters()
        self.reset_parameters()

    def flatten_parameters(self):
        """
        Resets parameter data pointer so that they can use faster code paths.
        Right now, this works only if the module is on the GPU and cuDNN is enabled.
        Otherwise, it's a no-op.
        """
        any_param = next(self.parameters()).data
        if not any_param.is_cuda or not torch.backends.cudnn.is_acceptable(any_param):
            return

        all_weights = self._flat_weights
        unique_data_ptrs = set(p.data_ptr() for p in all_weights)
        if len(unique_data_ptrs) != len(all_weights):
            return

        with torch.cuda.device_of(any_param):
            import torch.backends.cudnn.rnn as rnn

            with torch.no_grad():
                torch._cudnn_rnn_flatten_weight(
                    all_weights, (4 if self.bias else 2),
                    self.input_size, rnn.get_cudnn_mode(self.mode), self.hidden_size, self.num_layers,
                    self.batch_first, bool(self.bidirectional))

    def _apply(self, fn):
        ret = super(GRUD, self)._apply(fn)
        self.flatten_parameters()
        return ret

    def reset_parameters(self):
        stdv = 1.0 / math.sqrt(self.hidden_size)
        for weight in self.parameters():
            torch.nn.init.uniform_(weight, -stdv, stdv)

    def check_forward_args(self, input, hidden, batch_sizes):
        is_input_packed = batch_sizes is not None
        expected_input_dim = 2 if is_input_packed else 3
        if input.dim() != expected_input_dim:
            raise RuntimeError(
                'input must have {} dimensions, got {}'.format(
                    expected_input_dim, input.dim()))
        if self.input_size != input.size(-1):
            raise RuntimeError(
                'input.size(-1) must be equal to input_size. Expected {}, got {}'.format(
                    self.input_size, input.size(-1)))

        if is_input_packed:
            mini_batch = int(batch_sizes[0])
        else:
            mini_batch = input.size(0) if self.batch_first else input.size(1)

        num_directions = 2 if self.bidirectional else 1
        expected_hidden_size = (self.num_layers * num_directions,
                                mini_batch, self.hidden_size)

        def check_hidden_size(hx, expected_hidden_size, msg='Expected hidden size {}, got {}'):
            if tuple(hx.size()) != expected_hidden_size:
                raise RuntimeError(msg.format(expected_hidden_size, tuple(hx.size())))

        if self.mode == 'LSTM':
            check_hidden_size(hidden[0], expected_hidden_size,
                              'Expected hidden[0] size {}, got {}')
            check_hidden_size(hidden[1], expected_hidden_size,
                              'Expected hidden[1] size {}, got {}')
        else:
            check_hidden_size(hidden, expected_hidden_size)

    def extra_repr(self):
        s = '{input_size}, {hidden_size}'
        if self.num_layers != 1:
            s += ', num_layers={num_layers}'
        if self.bias is not True:
            s += ', bias={bias}'
        if self.batch_first is not False:
            s += ', batch_first={batch_first}'
        if self.dropout != 0:
            s += ', dropout={dropout}'
        if self.bidirectional is not False:
            s += ', bidirectional={bidirectional}'
        return s.format(**self.__dict__)

    def __setstate__(self, d):
        super(GRUD, self).__setstate__(d)
        if 'all_weights' in d:
            self._all_weights = d['all_weights']
        if isinstance(self._all_weights[0][0], str):
            return
        num_layers = self.num_layers
        num_directions = 2 if self.bidirectional else 1
        self._all_weights = []

        weights = ['weight_dg_x', 'weight_dg_h',
                   'weight_xz', 'weight_hz', 'weight_mz',
                   'weight_xr', 'weight_hr', 'weight_mr',
                   'weight_xh', 'weight_hh', 'weight_mh',
                   'weight_hy',
                   'bias_dg_x', 'bias_dg_h',
                   'bias_z', 'bias_r', 'bias_h', 'bias_y']

        if self.bias:
            self._all_weights += [weights]
        else:
            self._all_weights += [weights[:2]]

    @property
    def _flat_weights(self):
        return list(self._parameters.values())

    @property
    def all_weights(self):
        return [[getattr(self, weight) for weight in weights] for weights in self._all_weights]

    def forward(self, input, dataset_name='P12'):
        X = torch.squeeze(input[0])
        Mask = torch.squeeze(input[1])
        Delta = torch.squeeze(input[2])
        Hidden_State = torch.autograd.Variable(torch.zeros(self.input_size))

        output = None
        h = Hidden_State

        # decay rates gamma
        w_dg_x = getattr(self, 'weight_dg_x')
        w_dg_h = getattr(self, 'weight_dg_h')

        # z
        w_xz = getattr(self, 'weight_xz')
        w_hz = getattr(self, 'weight_hz')
        w_mz = getattr(self, 'weight_mz')

        # r
        w_xr = getattr(self, 'weight_xr')
        w_hr = getattr(self, 'weight_hr')
        w_mr = getattr(self, 'weight_mr')

        # h_tilde
        w_xh = getattr(self, 'weight_xh')
        w_hh = getattr(self, 'weight_hh')
        w_mh = getattr(self, 'weight_mh')

        # bias
        b_dg_x = getattr(self, 'bias_dg_x')
        b_dg_h = getattr(self, 'bias_dg_h')
        b_z = getattr(self, 'bias_z')
        b_r = getattr(self, 'bias_r')
        b_h = getattr(self, 'bias_h')

        for layer in range(self.num_layers):

            x = torch.squeeze(X[:, layer:layer + 1])
            m = torch.squeeze(Mask[:, layer:layer + 1])
            d = torch.squeeze(Delta[:, layer:layer + 1])

            # (4)
            gamma_x = torch.exp(-torch.max(self.zeros, (w_dg_x * d + b_dg_x)))
            gamma_h = torch.exp(-torch.max(self.zeros, (w_dg_h * d + b_dg_h)))

            # (5)
            x = m * x + (1 - m) * (gamma_x * x + (1 - gamma_x) * self.x_mean)

            # (6)
            if self.dropout == 0:
                h = gamma_h * h

                z = torch.sigmoid((w_xz * x + w_hz * h + w_mz * m + b_z))
                r = torch.sigmoid((w_xr * x + w_hr * h + w_mr * m + b_r))
                h_tilde = torch.tanh((w_xh * x + w_hh * (r * h) + w_mh * m + b_h))

                h = (1 - z) * h + z * h_tilde

            elif self.dropout_type == 'Moon':
                '''
                RNNDROP: a novel dropout for rnn in asr(2015)
                '''
                h = gamma_h * h

                z = torch.sigmoid((w_xz * x + w_hz * h + w_mz * m + b_z))
                r = torch.sigmoid((w_xr * x + w_hr * h + w_mr * m + b_r))

                h_tilde = torch.tanh((w_xh * x + w_hh * (r * h) + w_mh * m + b_h))

                h = (1 - z) * h + z * h_tilde
                dropout = torch.nn.Dropout(p=self.dropout)
                h = dropout(h)

            elif self.dropout_type == 'Gal':
                '''
                A Theoretically grounded application of dropout in recurrent neural networks(2015)
                '''
                dropout = torch.nn.Dropout(p=self.dropout)
                h = dropout(h)

                h = gamma_h * h

                z = torch.sigmoid((w_xz * x + w_hz * h + w_mz * m + b_z))
                r = torch.sigmoid((w_xr * x + w_hr * h + w_mr * m + b_r))
                h_tilde = torch.tanh((w_xh * x + w_hh * (r * h) + w_mh * m + b_h))

                h = (1 - z) * h + z * h_tilde

            elif self.dropout_type == 'mloss':
                '''
                recurrent dropout without memory loss arXiv 1603.05118
                g = h_tilde, p = the probability to not drop a neuron
                '''
                h = gamma_h * h

                z = torch.sigmoid((w_xz * x + w_hz * h + w_mz * m + b_z))
                r = torch.sigmoid((w_xr * x + w_hr * h + w_mr * m + b_r))
                h_tilde = torch.tanh((w_xh * x + w_hh * (r * h) + w_mh * m + b_h))

                dropout = torch.nn.Dropout(p=self.dropout)
                h_tilde = dropout(h_tilde)

                h = (1 - z) * h + z * h_tilde

            else:
                h = gamma_h * h

                z = torch.sigmoid((w_xz * x + w_hz * h + w_mz * m + b_z))
                r = torch.sigmoid((w_xr * x + w_hr * h + w_mr * m + b_r))
                h_tilde = torch.tanh((w_xh * x + w_hh * (r * h) + w_mh * m + b_h))

                h = (1 - z) * h + z * h_tilde

        w_hy = getattr(self, 'weight_hy')
        b_y = getattr(self, 'bias_y')

        output = torch.matmul(w_hy, h) + b_y
        if dataset_name == 'P12':
            output = torch.sigmoid(output)

        return output


class Raindrop(nn.Module):
    ""
    """Implement the raindrop stratey one by one."""
    """ Transformer model with context embedding, aggregation, split dimension positional and element embedding
    Inputs:
        d_inp = number of input features
        d_model = number of expected model input features
        nhead = number of heads in multihead-attention
        nhid = dimension of feedforward network model
        dropout = dropout rate (default 0.1)
        max_len = maximum sequence length 
        MAX  = positional encoder MAX parameter
        n_classes = number of classes 
    """

    def __init__(self, d_inp=36, d_model=64, nhead=4, nhid=128, nlayers=2, dropout=0.3, max_len=215, d_static=9,
                 MAX=100, perc=0.5, aggreg='mean', n_classes=2, global_structure = None, static=True):
        super(Raindrop, self).__init__()
        from torch.nn import TransformerEncoder, TransformerEncoderLayer
        self.model_type = 'Transformer'

        self.global_structure = global_structure

        d_pe = 36
        d_enc = 36

        self.pos_encoder = PositionalEncodingTF(d_pe, max_len, MAX)

        encoder_layers = TransformerEncoderLayer(d_model+36, nhead, nhid, dropout)  # nhid is the number of hidden, 36 is the dim of timestamp

        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)

        self.gcs = nn.ModuleList()
        conv_name = 'dense_hgt' # 'hgt' # 'dense_hgt',  'gcn', 'dense_hgt'
        num_types, num_relations = 36, 1

        self.edge_type_train = torch.ones([36*36*2], dtype=torch.int64).cuda()  # 2 times fully-connected graph
        self.adj = torch.ones([36, 36]).cuda()  # complete graph

        """For GIN"""
        self.dim = int(d_model/d_inp)  # the output dim of each node in graph
        in_D, hidden_D = 1, self.dim  # each timestamp input 2 value, map to self.dim dimension feature, output is self.dim
        self.GINstep1 = GINConv(
            Sequential(Linear(in_D, hidden_D), BatchNorm1d(hidden_D), ReLU(),
                       Linear(hidden_D, hidden_D), ReLU()))

        self.transconv  = TransformerConv(in_channels=36, out_channels=36*self.dim, heads=1)

        self.GINmlp = nn.Sequential(
            nn.Linear(self.dim+d_static, self.dim+d_static),  # self.dim for observation, d_static for static info
            nn.ReLU(),
            nn.Linear(self.dim+d_static, n_classes),
        )

        if static == False:
            d_final = d_enc + d_pe
        else:
            d_final = d_enc + d_pe + d_inp

        self.mlp_static = nn.Sequential(
            nn.Linear(d_final, d_final),
            nn.ReLU(),
            nn.Linear(d_final, n_classes),
        )

        self.d_inp = d_inp
        self.d_model = d_model
        self.encoder = nn.Linear(d_inp, d_enc)
        self.static = static
        if self.static:
            self.emb = nn.Linear(d_static, d_model)

        self.MLP_replace_transformer = nn.Linear(72, 36)

        self.mlp = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(),
            nn.Linear(d_model, n_classes),
        )

        self.aggreg = aggreg

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.init_weights()

    def init_weights(self):
        initrange = 1e-10
        self.encoder.weight.data.uniform_(-initrange, initrange)
        if self.static:
            self.emb.weight.data.uniform_(-initrange, initrange)

    def forward(self, src, static, times, lengths):
        """Input to the model:
        src = P: [215, 128, 36] : 36 nodes, 128 samples, each sample each channel has a feature with 215-D vector
        static = Pstatic: [128, 9]: this one doesn't matter; static features
        times = Ptime: [215, 128]: the timestamps
        lengths = lengths: [128]: the number of nonzero recordings.
        """
        missing_mask = src[:, :, self.d_inp:int(2*self.d_inp)]
        src = src[:, :, :self.d_inp]
        maxlen, batch_size = src.shape[0], src.shape[1]

        src = self.encoder(src) * math.sqrt(self.d_model)

        pe = self.pos_encoder(times)
        src = self.dropout(src)
        if static is not None:
            emb = self.emb(static)

        withmask = False
        if withmask==True:
            x = torch.cat([src, missing_mask], dim=-1)
        else:
            x = src

        # mask out the all-zero rows
        mask = torch.arange(maxlen)[None, :] >= (lengths.cpu()[:, None] )
        mask = mask.squeeze(1).cuda()

        step2 = True  # If skip step 2, direct process the input data
        if step2 == False:
            output = x
            distance = 0
        elif step2 == True:
            adj = self.global_structure.cuda()
            adj[torch.eye(36).byte()] = 1

            edge_index = torch.nonzero(adj).T
            edge_weights = adj[edge_index[0], edge_index[1]]

            # graph message passing
            output = torch.zeros([maxlen, src.shape[1], 36*self.dim]).cuda()
            alpha_all = torch.zeros([edge_index.shape[1],  src.shape[1] ]).cuda()
            for unit in range(0, x.shape[1]):
                stepdata = x[:, unit, :]
                stepdata, attentionweights = self.transconv(stepdata, edge_index=edge_index, edge_weights=edge_weights,
                                                            edge_attr=None, return_attention_weights=True)

                stepdata = stepdata.reshape([-1, 36*self.dim]).unsqueeze(0)
                output[:, unit, :] = stepdata

                alpha_all[:, unit] = attentionweights[1].squeeze(-1)

            # calculate Euclidean distance between alphas
            distance = torch.cdist(alpha_all.T, alpha_all.T, p=2)
            distance = torch.mean(distance)

        # use transformer to aggregate temporal information
        output = torch.cat([output, pe], dim=-1)

        step3 = True
        if step3==True:
            r_out = self.transformer_encoder(output, src_key_padding_mask=mask)
        elif step3==False:
            r_out = output

        masked_agg = True
        if masked_agg ==True:
            # masked aggregation across rows
            mask2 = mask.permute(1, 0).unsqueeze(2).long()
            if self.aggreg == 'mean':
                lengths2 = lengths.unsqueeze(1)
                output = torch.sum(r_out * (1 - mask2), dim=0) / (lengths2 + 1)
        elif masked_agg ==False:
            # Without masked aggregation across rows
            output = r_out[-1, :, :].squeeze(0)

        if static is not None:
            output = torch.cat([output, emb], dim=1)
        output = self.mlp_static(output)

        return output, distance, None

