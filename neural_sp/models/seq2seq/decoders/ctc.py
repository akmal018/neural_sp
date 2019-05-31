#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2019 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""CTC decoder."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import OrderedDict
from itertools import groupby
import logging
import numpy as np
import random
import torch
import torch.nn as nn
import torch.nn.functional as F

from neural_sp.models.criterion import kldiv_lsm_ctc
from neural_sp.models.modules.linear import LinearND
from neural_sp.models.seq2seq.decoders.decoder_base import DecoderBase
from neural_sp.models.torch_utils import np2tensor
from neural_sp.models.torch_utils import pad_list
from neural_sp.models.torch_utils import tensor2np

random.seed(1)

LOG_0 = float(np.finfo(np.float32).min)
LOG_1 = 0


class CTC(DecoderBase):
    """RNN Transducer.

    Args:
        eos (int): index for <eos> (shared with <sos>)
        blank (int): index for <blank>
        enc_n_units (int):
        vocab (int): number of nodes in softmax layer
        dropout (float): dropout probability for the RNN layer
        lsm_prob (float): label smoothing probability
        fc_list (list):
        param_init (float):

    """

    def __init__(self,
                 eos,
                 blank,
                 enc_n_units,
                 vocab,
                 dropout=0.0,
                 lsm_prob=0.0,
                 fc_list=[],
                 param_init=0.1):

        super(CTC, self).__init__()
        logger = logging.getLogger('training')

        self.lsm_prob = lsm_prob

        # Fully-connected layers before the softmax
        if len(fc_list) > 0:
            fc_layers = OrderedDict()
            for i in range(len(fc_list)):
                input_dim = enc_n_units if i == 0 else fc_list[i - 1]
                fc_layers['fc' + str(i)] = LinearND(input_dim, fc_list[i], dropout=dropout)
            fc_layers['fc' + str(len(fc_list))] = LinearND(fc_list[-1], vocab, dropout=0)
            self.output = nn.Sequential(fc_layers)
        else:
            self.output = LinearND(enc_n_units, vocab)

        import warpctc_pytorch
        self.warpctc_loss = warpctc_pytorch.CTCLoss(size_average=True)

    def reset_parameters(self, param_init):
        """Initialize parameters with uniform distribution."""
        logger = logging.getLogger('training')
        logger.info('===== Initialize %s =====' % self.__class__.__name__)
        for n, p in self.named_parameters():
            if p.dim() == 1:
                nn.init.constant_(p, val=0)  # bias
                logger.info('Initialize %s with %s / %.3f' % (n, 'constant', 0))
            elif p.dim() in [2, 4]:
                nn.init.uniform_(p, a=-param_init, b=param_init)
                logger.info('Initialize %s with %s / %.3f' % (n, 'uniform', param_init))
            else:
                raise ValueError

    def forward(self, eouts, elens, ys):
        """Compute CTC loss.

        Args:
            eouts (FloatTensor): `[B, T, dec_n_units]`
            elens (list): A list of length `[B]`
            ys (list): A list of length `[B]`, which contains a list of size `[L]`
        Returns:
            loss (FloatTensor): `[B, L, vocab]`

        """
        # Concatenate all elements in ys for warpctc_pytorch
        ylens = np2tensor(np.fromiter([len(y) for y in ys], dtype=np.int32))
        ys_ctc = torch.cat([np2tensor(np.fromiter(y, dtype=np.int32)) for y in ys], dim=0)
        # NOTE: do not copy to GPUs here

        # Compute CTC loss
        logits = self.output(eouts)
        loss = self.warpctc_loss(logits.transpose(1, 0).cpu(),  # time-major
                                 ys_ctc, elens.cpu(), ylens)
        # NOTE: ctc loss has already been normalized by bs
        # NOTE: index 0 is reserved for blank in warpctc_pytorch
        if self.device_id >= 0:
            loss = loss.cuda(self.device_id)

        # Label smoothing for CTC
        if self.lsm_prob > 0:
            loss = loss * (1 - self.lsm_prob) + kldiv_lsm_ctc(logits,
                                                              ylens=elens,
                                                              size_average=True) * self.lsm_prob

        return loss

    def greedy(self, eouts, elens):
        """Greedy decoding.

        Args:
            eouts (FloatTensor): `[B, T, enc_n_units]`
            elens (np.ndarray): `[B]`
        Returns:
            best_hyps (np.ndarray): Best path hypothesis. `[B, labels_max_seq_len]`

        """
        bs = eouts.size(0)
        best_hyps = []

        log_probs = F.log_softmax(self.output(eouts), dim=-1)

        # Pickup argmax class
        for b in range(bs):
            indices = []
            time = elens[b]
            for t in range(time):
                argmax = log_probs[b, t].argmax(0).item()
                indices.append(argmax)

            # Step 1. Collapse repeated labels
            collapsed_indices = [x[0] for x in groupby(indices)]

            # Step 2. Remove all blank labels
            best_hyp = [x for x in filter(lambda x: x != self.blank, collapsed_indices)]
            best_hyps.append(np.array(best_hyp))

        return np.array(best_hyps)

    def beam_search(self, eouts, elens, beam_width=1,
                    lm=None, lm_weight=0, length_penalty=0, lm_usage='rescoring'):
        """Beam search decoding.

        Args:
            eouts (FloatTensor): `[B, T, enc_n_units]`
            elens (list): A list of length `[B]`
            beam_width (int): the size of beam
            lm (RNNLM or GatedConvLM or TransformerLM):
            lm_weight (float): language model weight (the vocabulary is the same as CTC)
            length_penalty (float): insertion penalty
            lm_usage (str): rescoring or shallow_fusion
        Returns:
            best_hyps (list): Best path hypothesis. `[B, L]`

        """
        bs, _, vocab = eouts.size()
        best_hyps = []

        log_probs = F.log_softmax(self.output(eouts), dim=-1)

        for b in range(bs):
            # Elements in the beam are (prefix, (p_b, p_no_blank))
            # Initialize the beam with the empty sequence, a probability of
            # 1 for ending in blank and zero for ending in non-blank (in log space).
            beam = [{'hyp': [self.eos],  # <eos> is used for LM
                     'p_b': LOG_1,
                     'p_nb': LOG_0,
                     'clm_score': LOG_1,
                     'clm_hxs': None,
                     'clm_cxs': None}]

            for t in range(elens[b]):
                new_beam = []

                # Pick up the top-k scores
                log_probs_topk, indices_topk = torch.topk(
                    log_probs[b:b + 1, t], k=min(beam_width, vocab), dim=-1, largest=True, sorted=True)

                for i_beam in range(len(beam)):
                    hyp = beam[i_beam]['hyp']
                    p_b = beam[i_beam]['p_b']
                    p_nb = beam[i_beam]['p_nb']
                    clm_score = beam[i_beam]['clm_score']
                    clm_hxs = beam[i_beam]['clm_hxs']
                    clm_cxs = beam[i_beam]['clm_cxs']

                    # case 1. hyp is not extended
                    new_p_b = np.logaddexp(p_b + log_probs[b, t, self.blank].item(),
                                           p_nb + log_probs[b, t, self.blank].item())
                    if len(hyp) > 1:
                        new_p_nb = p_nb + log_probs[b, t, hyp[-1]].item()
                    else:
                        new_p_nb = LOG_0
                    new_beam.append({'hyp': hyp,
                                     'score': np.logaddexp(new_p_b, new_p_nb) + clm_score * lm_weight,
                                     'p_b': new_p_b,
                                     'p_nb': new_p_nb,
                                     'clm_score': clm_score,
                                     'clm_hxs': clm_hxs[:] if clm_hxs is not None else None,
                                     'clm_cxs': clm_cxs[:] if clm_cxs is not None else None})

                    # case 2. hyp is extended
                    new_p_b = LOG_0
                    for c in tensor2np(indices_topk)[0]:
                        p_t = log_probs[b, t, c].item()

                        if c == self.blank:
                            continue

                        last_token = hyp[-1] if len(hyp) > 1 else None
                        if c == last_token:
                            new_p_nb = p_b + p_t
                            # TODO(hirofumi): apply character LM here
                        else:
                            new_p_nb = np.logaddexp(p_b + p_t, p_nb + p_t)
                            # TODO(hirofumi): apply character LM here
                            if c == self.space:
                                pass
                                # TODO(hirofumi): apply word LM here

                        # Update LM states for shallow fusion
                        if lm_weight > 0 and lm is not None and lm_usage == 'shallow_fusion':
                            clmout, clmstate = lm.decode(
                                lm.encode(log_probs.new_zeros(1, 1).fill_(c).long()), (clm_hxs, clm_cxs))
                            clm_score = F.log_softmax(lm.generate(clmout), dim=-1)[0, 0, c]

                        new_beam.append({'hyp': beam[i_beam]['hyp'] + [c],
                                         'score': np.logaddexp(new_p_b, new_p_nb) + clm_score * lm_weight,
                                         'p_b': new_p_b,
                                         'p_nb': new_p_nb,
                                         'clm_score': clm_score,
                                         'clm_hxs': clm_hxs[:] if clm_hxs is not None else None,
                                         'clm_cxs': clm_cxs[:] if clm_cxs is not None else None})

                # Pruning
                beam = sorted(new_beam, key=lambda x: x['score'], reverse=True)[:beam_width]

            # Rescoing lattice
            if lm_weight > 0 and lm is not None and lm_usage == 'rescoring':
                new_beam = []
                device_id = torch.cuda.device_of(log_probs).idx
                for i_beam in range(len(beam)):
                    ys = [np2tensor(np.fromiter([self.eos] + beam[i_beam]['hyp'],
                                                dtype=np.int64), device_id).long()]
                    ys_pad = pad_list(ys, lm.pad)
                    clmout, _ = lm.decode(lm.encode(ys_pad), None)
                    clm_score = F.log_softmax(lm.generate(clmout), dim=-1).sum()
                    new_beam.append({'hyp': beam[i_beam]['hyp'],
                                     'score': np.logaddexp(beam[i_beam]['p_b'], beam[i_beam]['p_nb']) + clm_score * lm_weight})
                beam = sorted(new_beam, key=lambda x: x['score'], reverse=True)

            best_hyp = beam[0]['hyp'][1:]
            best_hyps.append(np.array(best_hyp))

        return np.array(best_hyps)


class CTCPrefixScore(object):
    """Compute CTC label sequence scores.

    which is based on Algorithm 2 in WATANABE et al.
    "HYBRID CTC/ATTENTION ARCHITECTURE FOR END-TO-END SPEECH RECOGNITION,"
    but extended to efficiently compute the probablities of multiple labels
    simultaneously

    [Reference]:
        https://github.com/espnet/espnet
    """

    def __init__(self, log_probs, blank, eos):
        """
        Args:
            log_probs ():
            blank (int): index of <blank>
            eos (int): index of <eos>

        """
        self.blank = blank
        self.eos = eos
        self.xlen = len(log_probs)
        self.log_probs = log_probs
        self.logzero = -10000000000.0

    def initial_state(self):
        """Obtain an initial CTC state

        :return: CTC state
        """
        # initial CTC state is made of a frame x 2 tensor that corresponds to
        # r_t^n(<sos>) and r_t^b(<sos>), where 0 and 1 of axis=1 represent
        # superscripts n and b (non-blank and blank), respectively.
        r = np.full((self.xlen, 2), self.logzero, dtype=np.float32)
        r[0, 1] = self.log_probs[0, self.blank]
        for i in range(1, self.xlen):
            r[i, 1] = r[i - 1, 1] + self.log_probs[i, self.blank]
        return r

    def __call__(self, hyp, cs, r_prev):
        """Compute CTC prefix scores for next labels.

        Args:
            hyp (list): prefix label sequence
            cs (np.ndarray): array of next labels. A tensor of size `[beam_width]`
            r_prev (np.ndarray): previous CTC state
        Returns:
            ctc_scores (np.ndarray):
            ctc_states (np.ndarray):
        """
        # initialize CTC states
        ylen = len(hyp) - 1  # ignore sos
        # new CTC states are prepared as a frame x (n or b) x n_labels tensor
        # that corresponds to r_t^n(h) and r_t^b(h).
        r = np.ndarray((self.xlen, 2, len(cs)), dtype=np.float32)
        xs = self.log_probs[:, cs]
        if ylen == 0:
            r[0, 0] = xs[0]
            r[0, 1] = self.logzero
        else:
            r[ylen - 1] = self.logzero

        # prepare forward probabilities for the last label
        r_sum = np.logaddexp(r_prev[:, 0], r_prev[:, 1])  # log(r_t^n(g) + r_t^b(g))
        last = hyp[-1]
        if ylen > 0 and last in cs:
            log_phi = np.ndarray((self.xlen, len(cs)), dtype=np.float32)
            for i in range(len(cs)):
                log_phi[:, i] = r_sum if cs[i] != last else r_prev[:, 1]
        else:
            log_phi = r_sum

        # compute forward probabilities log(r_t^n(h)), log(r_t^b(h)),
        # and log prefix probabilites log(psi)
        start = max(ylen, 1)
        log_psi = r[start - 1, 0]
        for t in range(start, self.xlen):
            # non-blank
            r[t, 0] = np.logaddexp(r[t - 1, 0], log_phi[t - 1]) + xs[t]
            # blank
            r[t, 1] = np.logaddexp(r[t - 1, 0], r[t - 1, 1]) + self.log_probs[t, self.blank]
            log_psi = np.logaddexp(log_psi, log_phi[t - 1] + xs[t])

        # get P(...eos|X) that ends with the prefix itself
        eos_pos = np.where(cs == self.eos)[0]
        if len(eos_pos) > 0:
            log_psi[eos_pos] = r_sum[-1]  # log(r_T^n(g) + r_T^b(g))

        # return the log prefix probability and CTC states, where the label axis
        # of the CTC states is moved to the first axis to slice it easily
        return log_psi, np.rollaxis(r, 2)