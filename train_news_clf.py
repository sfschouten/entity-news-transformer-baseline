import argparse
import pprint

import numpy as np
from datasets import load_dataset, load_metric
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, \
    Trainer, EarlyStoppingCallback, DataCollatorWithPadding

import mwep_dataset
from utils import create_run_folder_and_config_dict


def news_clf_dataset(config, tokenizer):
    # dataset processing/loading
    dataset = load_dataset(
        mwep_dataset.__file__,
        data_dir=config['nc_data_folder'],
        mwep_path=config['mwep_home'],
        eval_split_size=config['nc_eval_split_size'],
    ).flatten().remove_columns(
        [
            'uri',
            'incident.extra_info.sem:hasPlace',
            'incident.extra_info.sem:hasTimeStamp',
            'incident.wdt_id'
        ]
    ).rename_column('incident.incident_type', 'labels')

    # tokenize
    tokenized_dataset = dataset.map(
        lambda examples: tokenizer(examples['content'], padding=False, truncation=True),
        batched=True
    ).remove_columns(['content'])

    # count number of classes
    nr_classes = -1

    def calc_nr_labels(x):
        nonlocal nr_classes
        nr_classes = max(nr_classes, x['labels'] + 1)
    tokenized_dataset.map(calc_nr_labels)

    return tokenized_dataset, nr_classes


def train_news_clf(config):
    tokenizer = AutoTokenizer.from_pretrained(config['model'])
    tokenized_dataset, nr_classes = news_clf_dataset(config, tokenizer)

    # load model & metric
    model = AutoModelForSequenceClassification.from_pretrained(
        config['model'], num_labels=nr_classes
    )
    acc_metric = load_metric('accuracy')

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = np.argmax(logits, axis=-1)
        return acc_metric.compute(predictions=preds, references=labels)

    # training
    training_args = TrainingArguments(
        config['run_path'],
        fp16=True,
        num_train_epochs=config['max_nr_epochs'],
        per_device_train_batch_size=config['batch_size_train'],
        per_device_eval_batch_size=config['batch_size_eval'],
        gradient_accumulation_steps=config['gradient_acc_steps'],
        load_best_model_at_end=True,
        metric_for_best_model='accuracy',
        evaluation_strategy=config['eval_strategy'],
        save_strategy=config['eval_strategy'],
        eval_steps=config['eval_frequency'],
        warmup_steps=config['warmup_steps'],
        report_to=config['report_to'],
        save_total_limit=5,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_dataset['train'],
        eval_dataset=tokenized_dataset['validation'],
        compute_metrics=compute_metrics,
        callbacks=[
            EarlyStoppingCallback(early_stopping_patience=config['early_stopping_patience'])
        ],
        data_collator=DataCollatorWithPadding(
            tokenizer=tokenizer,
        ),
    )

    if config['probing']:
        for param in model.base_model.parameters():
            param.requires_grad = False

    if config['continue']:
        trainer.train(resume_from_checkpoint=config['checkpoint'])
    else:
        trainer.train()

    result = trainer.evaluate(tokenized_dataset['test'], metric_key_prefix='test')
    pprint.pprint(result)


if __name__ == "__main__":
    # parse cmdline arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--report_to', default=None, type=str)

    parser.add_argument('--nc_data_folder', default="../data/minimal")
    parser.add_argument('--nc_eval_split_size', default=500, type=int)

    parser.add_argument('--mwep_home', default='../mwep')
    parser.add_argument('--runs_folder', default='runs')
    parser.add_argument('--run_name', default=None)

    parser.add_argument('--model', default="distilbert-base-cased")
    parser.add_argument('--probing', action='store_true')

    parser.add_argument('--checkpoint', default=None)
    parser.add_argument('--continue', action='store_true')

    # parser.add_argument('--train_only', action='store_true')
    # parser.add_argument('--eval_only', action='store_true')

    parser.add_argument('--eval_strategy', default='steps', type=str)
    parser.add_argument('--eval_frequency', default=500, type=int)

    # hyper-parameters
    parser.add_argument('--max_nr_epochs', default=100, type=int)
    parser.add_argument('--warmup_steps', default=2000, type=int)
    parser.add_argument('--early_stopping_patience', default=5, type=int)
    parser.add_argument('--batch_size_train', default=64, type=int)
    parser.add_argument('--batch_size_eval', default=64, type=int)
    parser.add_argument('--gradient_acc_steps', default=1, type=int)
    # parser.add_argument('--learning_rate_base', default=1e-4, type=float)
    # parser.add_argument('--learning_rate_head', default=1e-3, type=float)

    args = parser.parse_args()
    train_news_clf(
        create_run_folder_and_config_dict(args)
    )
