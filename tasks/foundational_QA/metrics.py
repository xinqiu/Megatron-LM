
# The following code is adapted from
# https://github.com/facebookresearch/ParlAI/blob/master/parlai/core/metrics.py, 
# which is licensed under the MIT license. More details on the license can be 
# found at https://github.com/facebookresearch/ParlAI/blob/master/LICENSE.

# for scroll metrics https://huggingface.co/datasets/tau/scrolls/blob/6626d47eba7c33359d836ac262e2388f46e23bd1/metrics/f1.py
"""Provides standard metric evaluations for dialog."""

from collections import Counter
from typing import List
import numpy as np
import re
from nltk import ngrams

re_art = re.compile(r'\b(a|an|the)\b')
re_punc = re.compile(r'[!"#$%&()*+,-./:;<=>?@\[\]\\^`{|}~_\']')


def normalize_answer(s):
    """
    Lower text and remove punctuation, articles and extra whitespace.
    """
    s = s.lower()
    s = re_punc.sub(' ', s)
    s = re_art.sub(' ', s)
    s = ' '.join(s.split())
    return s


class F1Metric:
    """
    Helper class which computes token-level F1.
    """

    @staticmethod
    def _prec_recall_f1_score(pred_items, gold_items):
        """
        Compute precision, recall and f1 given a set of gold and prediction items.
        :param pred_items: iterable of predicted values
        :param gold_items: iterable of gold values
        :return: tuple (p, r, f1) for precision, recall, f1
        """
        common = Counter(gold_items) & Counter(pred_items)
        num_same = sum(common.values())
        if num_same == 0:
            return 0, 0, 0
        precision = 1.0 * num_same / len(pred_items)
        recall = 1.0 * num_same / len(gold_items)
        f1 = (2 * precision * recall) / (precision + recall)
        return precision, recall, f1

    @staticmethod
    def compute_each_pair(guess: str, answer: str, n=1):
        if answer == "":
            return None, None, None
        if guess == "":
            return 0, 0, 0
        g_tokens = normalize_answer(guess).split()
        a_tokens = normalize_answer(answer).split()
        g_tokens = list(ngrams(g_tokens, n))
        a_tokens = list(ngrams(a_tokens, n))
        precision, recall, f1 = F1Metric._prec_recall_f1_score(g_tokens, a_tokens)
        return precision, recall, f1

    @staticmethod
    def compute_all_pairs(guesses: List[str], answers: List[str], n=1):
        # additional augment:
        assert len(guesses) == len(answers)

        precision_list, recall_list, f1_list = [], [], []
        for guess, answer in zip(guesses, answers):
            precision, recall, f1 = F1Metric.compute_each_pair(guess, answer, n)
            if precision is None or recall is None or f1 is None:
                continue
            precision_list.append(precision)
            recall_list.append(recall)
            f1_list.append(f1)

        return np.mean(precision_list), np.mean(recall_list), np.mean(f1_list)

import regex
import string

def normalize_answer_squad(s: str) -> str:
    """Normalization from the SQuAD evaluation script.

    See https://worksheets.codalab.org/rest/bundles/0x6b567e1cf2e041ec80d7098f031c5c9e/contents/blob/
    """

    def remove_articles(text):
        return regex.sub(r"\b(a|an|the)\b", " ", text)

    def white_space_fix(text):
        return " ".join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def best_subspan_em(prediction: str, ground_truths: List[str]) -> float:
    normalized_prediction = normalize_answer_squad(prediction)

    for ground_truth in ground_truths:
        normalized_ground_truth = normalize_answer_squad(ground_truth)
        if normalized_ground_truth.lower() in normalized_prediction.lower():
            return 1.0
    return 0.0


def get_em_lost_in_the_middle(predictions: List[str], ground_truths_list: List[List[str]]) -> float:
    """Get the EM metric used in the 'lost in the middle' paper."""
    total_correct = 0
    for prediction, ground_truths in zip(predictions, ground_truths_list):
        total_correct += best_subspan_em(prediction, ground_truths)
    return total_correct / len(predictions)
