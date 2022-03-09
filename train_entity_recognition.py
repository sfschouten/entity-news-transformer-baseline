import argparse
from functools import partial

import numpy as np

from datasets import load_dataset, load_metric, DatasetDict
from transformers import AutoTokenizer, BatchEncoding, AutoModelForTokenClassification, \
    TrainingArguments, Trainer, EarlyStoppingCallback, DataCollatorForTokenClassification

from utils import create_run_folder_and_config_dict
import el_wiki_dataset

I, O, B = 0, 1, 2


def kilt_for_er_dataset(config, tokenizer):
    def construct_iob_labels(example, batch_encoding: BatchEncoding):
        def warn(s_t, e_t, s_c, e_c):
            print(f"\nWARNING: NoneType ..."
                  f"\nwith start_token={s_t}, end_token={e_t} "
                  f"\nfor start_char={s_c}, end_char={e_c} "
                  f"\nfor text: {example['mentioning_text']}")

        labels = [O] * len(batch_encoding['input_ids'])
        start_chars = example['mentions']['start_char']
        end_chars = example['mentions']['end_char']
        for start_char, end_char in zip(start_chars, end_chars):
            if start_char < 0 or end_char < 0:
                warn(-1, -1, start_char, end_char)
                continue
            start_token = batch_encoding.char_to_token(start_char)
            end_token = batch_encoding.char_to_token(end_char - 1)
            if start_token is None or end_token is None:
                warn(start_token, end_token, start_char, end_char)
                continue
            labels[start_token] = B
            for t in range(start_token + 1, end_token):
                labels[t] = I

        batch_encoding['labels'] = labels
        return batch_encoding

    kwargs = {}
    if config['er_dataset_size']:
        kwargs['max_samples'] = config['er_dataset_size']
    dataset = load_dataset(
        el_wiki_dataset.__file__,
        split='full',
        **kwargs
    )

    # tokenize
    tokenized_dataset = dataset.map(
        lambda example: construct_iob_labels(
            example,
            tokenizer(
                example['mentioning_text'],
                truncation=True
            )
        ), batched=False
    ).remove_columns(['mentioning_text', 'mentions'])

    return tokenized_dataset


def conll2003_dataset(config, tokenizer):
    TAGS = ['O', 'B-PER', 'I-PER', 'B-ORG', 'I-ORG', 'B-LOC', 'I-LOC', 'B-MISC', 'I-MISC']

    def labels(example, batch_encoding: BatchEncoding):
        words = batch_encoding.words()  # maps tokens to word indices
        labels = [TAGS[example['ner_tags'][w]][0] if w else 'O' for w in words]
        labels = [{'I': 0, 'O': 1, 'B': 2}[lbl] for lbl in labels]
        batch_encoding['labels'] = labels
        return batch_encoding

    dataset = load_dataset("conll2003")
    dataset = dataset.map(
        lambda example: labels(
            example,
            tokenizer(
                example['tokens'],
                is_split_into_words=True,
                truncation=True
            )
        ), batched=False,
    ).remove_columns(['tokens'])
    return dataset


def compute_er_metrics(seq_metric, eval_pred):
    def swap4lbl(ndarray):
        return [
            [{I: 'I', O: 'O', B: 'B'}[x.item()] for x in row if x.item() != -100]
            for row in ndarray
        ]

    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    labels = swap4lbl(labels)
    preds = swap4lbl(preds)
    preds = [pred[:len(lbl)] for pred, lbl in zip(preds, labels)]
    er_result = seq_metric.compute(predictions=preds, references=labels, scheme='IOB2')
    return er_result


def train_entity_linking(config):
    tokenizer = AutoTokenizer.from_pretrained(config['model'])

    # kilt dataset
    kilt_dataset = kilt_for_er_dataset(config, tokenizer)
    train_eval = kilt_dataset.train_test_split(test_size=0.01)
    valid_test = train_eval['test'].train_test_split(test_size=0.5)
    kilt_dataset = DatasetDict({
        'train': train_eval['train'],
        'validation': valid_test['train'],
        'test':  valid_test['test']
    })

    # conll2003 dataset
    conll_dataset = conll2003_dataset(config, tokenizer)

    # load model
    model = AutoModelForTokenClassification.from_pretrained(config['model'], num_labels=3)

    training_args = TrainingArguments(
        config['run_path'],
        fp16=True,
        evaluation_strategy="steps",
        num_train_epochs=config['max_nr_epochs'],
        per_device_train_batch_size=config['batch_size_train'],
        per_device_eval_batch_size=config['batch_size_eval'],
        gradient_accumulation_steps=config['gradient_acc_steps'],
        load_best_model_at_end=True,
        metric_for_best_model='overall_f1',
        eval_steps=500,
        max_steps=1000000,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=kilt_dataset['train'],
        eval_dataset=conll_dataset['validation'],
        compute_metrics=partial(compute_er_metrics, load_metric('seqeval')),
        data_collator=DataCollatorForTokenClassification(
            tokenizer=tokenizer,
        ),
        callbacks=[
            EarlyStoppingCallback(early_stopping_patience=config['early_stopping_patience'])
        ]
    )
    if config['continue']:
        trainer.train(resume_from_checkpoint=config['checkpoint'])
    else:
        trainer.train()

    result = trainer.evaluate(conll_dataset['test'])
    print(result)


if __name__ == "__main__":
    # parse cmdline arguments
    parser = argparse.ArgumentParser()

    parser.add_argument('--runs_folder', default='runs')
    parser.add_argument('--run_name', default=None)

    parser.add_argument('--model', default="distilbert-base-cased")

    parser.add_argument('--checkpoint', default=None)
    parser.add_argument('--continue', action='store_true')

    # parser.add_argument('--train_only', action='store_true')
    # parser.add_argument('--eval_only', action='store_true')

    parser.add_argument('--er_dataset_size', default=None, type=int)

    # hyper-parameters
    parser.add_argument('--max_nr_epochs', default=100, type=int)
    parser.add_argument('--early_stopping_patience', default=5, type=int)
    parser.add_argument('--batch_size_train', default=64, type=int)
    parser.add_argument('--batch_size_eval', default=64, type=int)
    parser.add_argument('--gradient_acc_steps', default=1, type=int)
    # parser.add_argument('--learning_rate_base', default=1e-4, type=float)
    # parser.add_argument('--learning_rate_head', default=1e-3, type=float)

    args = parser.parse_args()
    train_entity_linking(
        create_run_folder_and_config_dict(args)
    )
