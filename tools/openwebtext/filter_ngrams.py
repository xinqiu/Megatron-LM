# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Deduplicate downstream tasks from training dataset. 13-grams have been used.
All split documents with less than 200 characters got filtered. Any document
with more than 10 splits got filtered as well.
"""

import argparse
from functools import partial
import json
import multiprocessing
import nltk
import re
import string
import sys
import time

def get_words(text):
    # get all the lowercase words from text
    words, positions = [], []
    for match in re.finditer(r'\w+', text.lower()):
        words.append(match.group(0))
        positions.append(match.start())
    return words, positions

# splits the text
def split_text(text, start_position, remove_char_each_side, seq):
    # first part of the text
    punctuations = ".!?"
    pos = start_position - remove_char_each_side
    text_first = ""
    while pos > 0 and not text[pos] in punctuations:
        pos -= 1
    if pos > 0:
        text_first = text[0:pos+1]

    # add length of seq and remove_char_each_side
    pos = start_position + len(seq) + remove_char_each_side

    # last part of the text
    text_second = ""
    while pos < len(text) and not text[pos] in punctuations:
        pos += 1
    if pos + 1 < len(text):
        text_second = text[pos+1:len(text)]

    return text_first, text_second

def check_and_clean_text(args, words, ngrams, text, start_position, \
    text_buf_ngram_free, text_buf):

    seq = " ".join(words)
    if seq in ngrams:
        print(" [matched]: {}".format(seq), flush=True)

        # split the text
        text_first, text_second = split_text(text, start_position, \
            args.remove_char_each_side, seq)

        # first part of ngrams free
        if len(text_first) > args.filter_text_char_len:
            text_buf_ngram_free.append(text_first)

        # add second part for further processing
        if len(text_second) > args.filter_text_char_len:
            text_buf.append(text_second)

        return False # not ngram free

    # ngram free
    return True

def free_ngram(line, args, key, ngrams, ngrams_freq_sorted):
    # remove all the ngrams

    try:
        myjson = json.loads(line)
        text_buf = [myjson[key]]
    except Exception as e:
        print("Error: {}".format(e), flush=True)
        text_buf = []

    text_buf_ngram_free = []
    while len(text_buf) > 0:

        # get the first one from the buffer
        text = text_buf.pop(0)
        words, positions = get_words(text)

        ngram_free = True
        # find each max n-grams and check dictionary
        for i in range(len(words) - args.ngram_size + 1):
            check_ngram_free = check_and_clean_text(args, words[i:\
                i+args.ngram_size], ngrams, text, positions[i], \
                text_buf_ngram_free, text_buf)

            # the seq is ngram free? if yes, break
            if not check_ngram_free:
                ngram_free = False
                break

            # if max ngrams doesn't match, check if any other lower n-grams
            # within max ngram macthes
            for ngram_len, _ in ngrams_freq_sorted:
                check_ngram_free = check_and_clean_text(args, words[i:\
                    i+ngram_len], ngrams, text, positions[i], \
                    text_buf_ngram_free, text_buf)

                # same check as above
                if not check_ngram_free:
                    ngram_free = False
                    break

            # check break from lower than max ngram loop above
            if not ngram_free:
                break

        # for the last max n-gram, check all the lower ngrams in it
        if ngram_free and len(words) - args.ngram_size > 0:
            # get the last words of the lax max ngram
            last_seq_words = words[(len(words) - args.ngram_size):len(words)]
            last_seq_start_position = len(words) - args.ngram_size

            # check all n-grams lower than the max
            for pos, (ngram_len, _) in enumerate(ngrams_freq_sorted):

                # ignore the max ngram as has been considered already
                if ngram_len == args.ngram_size:
                    continue

                # find each ngram of ngram_len in max n-grams and check
                for i in range(len(last_seq_words) - ngram_len + 1):
                    check_ngram_free = check_and_clean_text(args, \
                        last_seq_words[i:i+ngram_len], ngrams, text,\
                        positions[last_seq_start_position+i], \
                        text_buf_ngram_free, text_buf)

                    if not check_ngram_free:
                        ngram_free = False
                        break

                if not ngram_free:
                    break

        # texts are ngram free
        if ngram_free:
            text_buf_ngram_free.append(text)

    # check if the text has only been trimmed
    trimmed = 0
    if len(text_buf_ngram_free) == 1 and len(text_buf_ngram_free[0]) == \
        len(myjson[key]):
        trimmed = 1

    return text_buf_ngram_free, trimmed

# insert word sequence into dictionary
def insert_dict(words, ngrams, pos):
    seq = " ".join(words)
    if seq not in ngrams:
        ngrams[seq] = pos

# insert each ngram from text into the ngrams dictionary
def compute_ngrams_insert_dict(args, text, ngrams):
    words, positions = get_words(text)
    if len(words) == 0:
        return

    if len(words) < args.ngram_size:
        insert_dict(words, ngrams, positions[0])

    for i in range(len(words) - args.ngram_size+1):
        insert_dict(words[i:i+args.ngram_size], ngrams, positions[i])


# Build ngrams for the lambada dataset
def process_task_lambda(args, task_file, ngrams):
    print(' reading from {} and computing ngrams'.format(task_file))
    with open(task_file, 'r') as f:
        for line in f:
            try:
                myjson = json.loads(line)
                text = myjson['text']
                compute_ngrams_insert_dict(args, text, ngrams)
            except Exception as e:
                print('Error:', e)
    print(" Entities in ngrams {}".format(len(ngrams)), flush=True)


# Build ngrams for the squad v2 dataset
def process_task_squad(args, ngrams):
    print(' reading from {} and computing ngrams'.format('import datasets'))
    # using squad data from datasets
    from datasets import load_dataset
    squad_v2 = load_dataset('squad_v2', split='validation')

    for line in squad_v2:
        try:
            text = line['question']
            compute_ngrams_insert_dict(args, text, ngrams)
        except Exception as e:
            print('Error:', e)
    print(" Entities in ngrams {}".format(len(ngrams)), flush=True)


if __name__ == '__main__':

    # we use 13-grams, any text less than 200 characters got removed
    # any text splitted more than 10 got removed as well

    print('parsing the arguments ...')

    parser = argparse.ArgumentParser()
    parser.add_argument('--tasks', nargs = '*', required=True, default=None, \
                        help = 'Tasks to use for deduplication: currently '
                        ' suuport [lambada, squad]')
    parser.add_argument('--lambada-path', type=str, default=None,
                       help='Only Lambada task needs the path')
    parser.add_argument('--dedup-dataset', nargs = '*', default=None,
                       help='Dataset to deduplicate with the key to use'
                        ' e.g. cc.json text')
    parser.add_argument('--output', type=str, default=None,
                       help='Output file name to save dedup dataset')
    # Default dedup values
    parser.add_argument('--ngram-size', type=int, default=13,
                       help='Maximum size of ngram to use.')
    parser.add_argument('--filter-text-char-len', type=int, default=200,
                       help='Remove any text below this length.')
    parser.add_argument('--splits-count', type=int, default=10,
                       help='Remove any documents more than this many splits')
    parser.add_argument('--remove-char-each-side', type=int, default=200,
                       help='Maximum size of ngram to use.')

    args = parser.parse_args()

    # Build ngrams
    ngrams = {}
    for _, task_name in enumerate(args.tasks):
        print('Task: {}'.format(task_name), flush=True)
        if task_name == 'lambada':
            assert args.lambada_path is not None
            process_task_lambda(args, args.lambada_path, ngrams)
        if task_name == 'squad':
            process_task_squad(args, ngrams)

    # get the range of the size of the ngrams
    ngrams_freq = {}
    for ngram_key in ngrams.keys():
        length = len(ngram_key.split())
        ngrams_freq[length] = ngrams_freq[length] + 1 if length in \
            ngrams_freq else 1
    ngrams_freq_sorted = sorted(ngrams_freq.items(), key=lambda item: item[1])

    print(" Ngram frequencies: {}".format(ngrams_freq_sorted), flush=True)
    print(" Entities in ngrams {} min_ngram_size {} max_ngram_size {}".format(\
            len(ngrams), ngrams_freq_sorted[0][0], ngrams_freq_sorted[len(\
            ngrams_freq_sorted) -1 ][0]), flush=True)

    id_prefix = '-'.join(args.tasks[::2])

    print('Reading file {} and deduping n-grams'.format(args.dedup_dataset))

    counter = 0
    start_time = time.time()
    out_f = open(args.output, 'wb')
    splitted, ignored, split_mt_thld, trimmed_count = 0, 0, 0, 0

    assert len(args.dedup_dataset) == 2
    dedup_file = args.dedup_dataset[0]
    dedup_key = args.dedup_dataset[1]

    # Setup multi-processing.
    num_workers = 40
    fin = open(dedup_file, 'r', encoding='utf-8')
    pool = multiprocessing.Pool(num_workers)
    free_ngram_x=partial(free_ngram, args=args, key=dedup_key, ngrams=ngrams, \
        ngrams_freq_sorted=ngrams_freq_sorted)

    free_ngrams = pool.imap(free_ngram_x, fin, 25)

    for text_buf_ngram_free, trimmed in free_ngrams:
        counter += 1
        try:

            trimmed_count += trimmed

            if len(text_buf_ngram_free) > 1:
                splitted += (len(text_buf_ngram_free) - 1)
            if len(text_buf_ngram_free) == 0:
                ignored += 1
            # more than 10 splits ignored
            if len(text_buf_ngram_free) > args.splits_count:
                text_buf_ngram_free = []
                split_mt_thld += 1

            for i in range(len(text_buf_ngram_free)):
                split_id_string = id_prefix + '-{:010d}'.format(int(counter)) \
                    + '-{:010d}'.format(int(i))
                outjson = json.dumps({"text":text_buf_ngram_free[i], 
                    id_prefix+"_split_id":split_id_string},
                    ensure_ascii=False)
                out_f.write(outjson.encode('utf-8'))
                out_f.write('\n'.encode('utf-8'))

            if counter % 1000 == 0:
                print(' [search]> processed {} documents in {:.2f} seconds ...'.
                    format(counter, time.time() - start_time), flush=True)
        except Exception as e:
            print('Error:', e)

    out_f.close()
    fin.close()

    print("Deduped file written to: {}".format(args.output), flush=True)
    print("Total docs {} splitted {} ignored {} docs with many splits {}"\
        " trimmed {}".format(counter, splitted, ignored, split_mt_thld, \
        trimmed_count), flush=True)
    print('done :-)')
