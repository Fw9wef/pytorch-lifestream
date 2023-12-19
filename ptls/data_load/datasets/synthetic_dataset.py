import logging
import os
import warnings
from glob import glob
from itertools import chain
from typing import Union, List

import numpy as np
import torch
from omegaconf import ListConfig

from ptls.data_load import read_pyarrow_file
from ptls.data_load import IterableChain

logger = logging.getLogger(__name__)


class Dist:
    def sample(self):
        raise NotImplemented


class BetaDist(Dist):
    def __init__(self, a, b, **kwargs):
        self.a = a
        self.b = b

    def sample(self):
        return np.random.beta(self.a, self.b)


class UniformDist(Dist):
    def __init__(self, **kwargs):
        pass

    def sample(self):
        return np.random.rand()


class ConstDist(Dist):
    def __init__(self, n, p, **kwargs):
        assert n == len(p)
        self.n = n
        self.p = p

    def sample(self):
        return np.random.choice(self.n, p=self.p) / self.n


class Feature:
    def sample(self):
        raise NotImplemented


class CategoryFeature(Feature):
    def __init__(self, n, dist_type='uniform', dist_args={}):
        assert dist_type in ['uniform', 'beta', 'const']
        self.n = n

        if dist_type == 'uniform':
            self.dist = UniformDist()
        elif dist_type == 'beta':
            self.dist = BetaDist(**dist_args)
        elif dist_type == 'const':
            self.dist = ConstDist(n=n, **dist_args)

    def sample(self):
        return int(self.dist.sample() * self.n)


class FloatFeature(Feature):
    def __init__(self, min=0, max=1, log=False, dist_type='uniform', dist_args={}):
        assert dist_type in ['uniform', 'beta']
        self.min = min
        self.max = max
        self.log = log

        if dist_type == 'uniform':
            self.dist = UniformDist()
        elif dist_type == 'beta':
            self.dist = BetaDist(**dist_args)

    def sample(self):
        v = self.min + self.dist.sample() * (self.max - self.min)
        v = np.exp(v) if self.log else v
        return v



class State:
    def __init__(self, features, ind):
        self.features = features
        self.names = set(self.features.keys())
        self.ind = ind

    def unwrap(self):
        return {k: v.sample() for k, v in self.features.items()}


class HMM:
    def __init__(self, states, hidden_states, state_transition_tensor, hidden_state_transition_matrix, noise=0.):
        self.states = states
        self.n_states = len(self.states)
        self.hidden_states = hidden_states
        self.n_hidden_states = len(self.hidden_states)
        self.state_transition_tensor = state_transition_tensor
        self.hidden_state_transition_matrix = hidden_state_transition_matrix
        self.noise = noise

        assert self.state_transition_tensor.ndim == 3
        assert self.hidden_state_transition_matrix.ndim == 2
        assert self.state_transition_tensor.shape[0] == self.state_transition_tensor.shape[1] == self.n_states
        assert self.state_transition_tensor.shape[2] == self.hidden_state_transition_matrix.shape[0]
        assert self.hidden_state_transition_matrix.shape[0] == \
               self.hidden_state_transition_matrix.shape[1] == \
               self.n_hidden_states
        assert self.check_states_for_inds(self.states)
        assert self.check_states_for_inds(self.hidden_states)

        self.state = None
        self.h_state = None
        self.reset()

    def gen_next(self):
        self.h_state = np.random.choice(self.hidden_states, p=self.hidden_state_transition_matrix[self.h_state.ind])
        self.state = np.random.choice(self.states, p=self.state_transition_tensor[self.state.ind, :, self.h_state.ind])
        h_state = self.h_state if np.random.rand() >= self.noise else np.random.choice(self.hidden_states)
        return h_state, self.state

    def gen_seq(self, l=100):
        assert l > 1
        a, b = list(), list()
        hs, s = self.reset()
        a.append(hs.unwrap())
        b.append(s.unwrap())

        for i in range(l-1):
            hs, s = self.gen_next()
            a.append(hs.unwrap())
            b.append(s.unwrap())

        a = {k: torch.Tensor([x[k] for x in a]) for k in a[0]}
        b = {k: torch.Tensor([x[k] for x in b]) for k in b[0]}
        return {**a, **b, 'event_time': torch.arange(l)}

    def reset(self):
        self.h_state = np.random.choice(self.hidden_states)
        self.state = np.random.choice(self.states)
        return self.h_state, self.state

    @staticmethod
    def check_states_for_inds(states):
        return sorted([s.ind for s in states]) == [i for i in range(len(states))]


class SyntheticDataset(torch.utils.data.Dataset):
    def __init__(self,
                 hmms,
                 seq_len=1000,
                 dataset_size=10000,
                 post_processing=None,
                 i_filters: List = None):
        self.hmms = hmms
        self.seq_len = seq_len
        self.dataset_size = dataset_size
        if i_filters is not None:
            self.post_processing = IterableChain(*i_filters)
        else:
            self.post_processing = post_processing
        if post_processing is not None:
            warnings.warn('`post_processing` parameter is deprecated, use `i_filters`')

    def __getitem__(self, item):
        c = item % len(self.hmms)
        hmm = self.hmms[c]
        item = hmm.gen_seq(self.seq_len)
        item['class_label'] = c
        if self.post_processing is not None:
            item = self.post_processing(item)
        return item

    def __len__(self):
        return self.dataset_size

    @staticmethod
    def to_torch(x):
        if type(x) is np.ndarray and x.dtype.kind in ('i', 'f'):
            return torch.from_numpy(x)
        return x
