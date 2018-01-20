#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Evaluate the trained model (Librispeech corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join, abspath
import sys
import argparse

sys.path.append(abspath('../../../'))
from models.load_model import load
from examples.librispeech.data.load_dataset import Dataset
from examples.librispeech.metrics.cer import do_eval_cer
from examples.librispeech.metrics.wer import do_eval_wer
from utils.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument('--model_path', type=str,
                    help='path to the model to evaluate')
parser.add_argument('--epoch', type=int, default=-1,
                    help='the epoch to restore')
parser.add_argument('--beam_width', type=int, default=1,
                    help='beam_width (int, optional): beam width for beam search.' +
                    ' 1 disables beam search, which mean greedy decoding.')
parser.add_argument('--eval_batch_size', type=int, default=1,
                    help='the size of mini-batch in evaluation')
parser.add_argument('--max_decode_len', type=int, default=600,  # or 100
                    help='the length of output sequences to stop prediction when EOS token have not been emitted')


def main():

    args = parser.parse_args()

    # Load a config file (.yml)
    params = load_config(join(args.model_path, 'config.yml'))

    # Load model
    model = load(model_type=params['model_type'],
                 params=params,
                 backend=params['backend'])

    # GPU setting
    model.set_cuda(deterministic=False, benchmark=True)

    # Restore the saved model
    checkpoint = model.load_checkpoint(
        save_path=args.model_path, epoch=args.epoch)
    model.load_state_dict(checkpoint['state_dict'])

    # Load dataset
    vocab_file_path = '../metrics/vocab_files/' + \
        params['label_type'] + '_' + params['data_size'] + '.txt'
    test_clean_data = Dataset(
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        model_type=params['model_type'],
        data_type='test_clean', data_size=params['data_size'],
        label_type=params['label_type'], vocab_file_path=vocab_file_path,
        batch_size=args.eval_batch_size, splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        sort_utt=False, save_format=params['save_format'])
    test_other_data = Dataset(
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        model_type=params['model_type'],
        data_type='test_other', data_size=params['data_size'],
        label_type=params['label_type'], vocab_file_path=vocab_file_path,
        batch_size=args.eval_batch_size, splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        sort_utt=False, save_format=params['save_format'])

    print('=== Test Data Evaluation ===')
    if 'word' in params['label_type']:
        wer_test_clean = do_eval_wer(
            model=model,
            model_type=params['model_type'],
            dataset=test_clean_data,
            label_type=params['label_type'],
            beam_width=args.beam_width,
            max_decode_len=args.max_decode_len,
            eval_batch_size=args.eval_batch_size,
            progressbar=True)
        print('  WER (clean): %f %%' % (wer_test_clean * 100))
        wer_test_other = do_eval_wer(
            model=model,
            model_type=params['model_type'],
            dataset=test_other_data,
            label_type=params['label_type'],
            beam_width=args.beam_width,
            max_decode_len=args.max_decode_len,
            eval_batch_size=args.eval_batch_size,
            progressbar=True)
        print('  WER (other): %f %%' % (wer_test_other * 100))
        print('  WER (mean): %f %%' %
              ((wer_test_clean + wer_test_other) * 100 / 2))
    else:
        cer_test_clean, wer_test_clean = do_eval_cer(
            model=model,
            model_type=params['model_type'],
            dataset=test_clean_data,
            label_type=params['label_type'],
            beam_width=args.beam_width,
            max_decode_len=args.max_decode_len,
            eval_batch_size=args.eval_batch_size,
            progressbar=True)
        print('  CER (clean): %f %%' % (cer_test_clean * 100))
        print('  WER (clean): %f %%' % (wer_test_clean * 100))
        cer_test_other, wer_test_other = do_eval_cer(
            model=model,
            model_type=params['model_type'],
            dataset=test_other_data,
            label_type=params['label_type'],
            beam_width=args.beam_width,
            max_decode_len=args.max_decode_len,
            eval_batch_size=args.eval_batch_size,
            progressbar=True)
        print('  CER (other): %f %%' % (cer_test_other * 100))
        print('  WER (other): %f %%' % (wer_test_other * 100))
        print('  CER (mean): %f %%' %
              ((cer_test_clean + cer_test_other) * 100 / 2))
        print('  WER (mean): %f %%' %
              ((wer_test_clean + wer_test_other) * 100 / 2))


if __name__ == '__main__':
    main()
