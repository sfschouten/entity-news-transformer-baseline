import argparse
import os
import json
import datetime
import pprint

import numpy as np
from datasets import load_dataset, load_metric
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, \
    Trainer, EarlyStoppingCallback

import mwep_dataset

# from finetune import fine_tune, evaluate, LAST_CHECKPOINT_NAME
# from model import Model


# parse cmdline arguments
parser = argparse.ArgumentParser()
parser.add_argument('--data_folder', default="../data/minimal/bin")

parser.add_argument('--mwep_home', default='../mwep')
parser.add_argument('--runs_folder', default='runs')
parser.add_argument('--run_name', default=None)

parser.add_argument('--model', default="distilbert-base-cased")

parser.add_argument('--checkpoint', default=None)
parser.add_argument('--continue', action='store_true')

# parser.add_argument('--train_only', action='store_true')
# parser.add_argument('--eval_only', action='store_true')


# hyper-parameters
parser.add_argument('--max_nr_epochs', default=100, type=int)
parser.add_argument('--early_stopping_patience', default=5, type=int)
parser.add_argument('--batch_size_train', default=64, type=int)
parser.add_argument('--batch_size_eval', default=64, type=int)
# parser.add_argument('--learning_rate_base', default=1e-4, type=float)
# parser.add_argument('--learning_rate_head', default=1e-3, type=float)

args = parser.parse_args()

# create run folder
if args.run_name:
    run_name = args.run_name
else:
    run_name = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
run_path = os.path.join(args.runs_folder, run_name)
os.makedirs(run_path)

# config dict
config = {**vars(args), 'run_path': run_path}
with open(os.path.join(run_path, 'config.json'), 'w') as fp:
    json.dump(config, fp)

# dataset processing/loading
dataset = load_dataset(
    mwep_dataset.__file__,
    data_dir=config['data_folder'],
    mwep_path=config['mwep_home'],
).flatten().remove_columns(
    [
        'uri',
        'incident.extra_info.sem:hasPlace',
        'incident.extra_info.sem:hasTimeStamp',
        'incident.wdt_id'
    ]
).rename_column('incident.incident_type', 'label')

tokenizer = AutoTokenizer.from_pretrained(config['model'])

tokenized_dataset = dataset.map(
    lambda examples: tokenizer(examples['content'], padding='max_length', truncation=True),
    batched=True
).remove_columns(['content'])

model = AutoModelForSequenceClassification.from_pretrained(config['model'], num_labels=4)

acc_metric = load_metric('accuracy')


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return acc_metric.compute(predictions=preds, references=labels)


training_args = TrainingArguments(
    run_path,
    fp16=True,
    evaluation_strategy="steps",
    num_train_epochs=config['max_nr_epochs'],
    per_device_train_batch_size=config['batch_size_train'],
    per_device_eval_batch_size=config['batch_size_eval'],
    load_best_model_at_end=True,
    eval_steps=500,
)
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_dataset['train'],
    eval_dataset=tokenized_dataset['validation'],
    compute_metrics=compute_metrics,
    callbacks=[
        EarlyStoppingCallback(early_stopping_patience=config['early_stopping_patience'])
    ]
)
if config['continue']:
    trainer.train(resume_from_checkpoint=config['checkpoint'])
else:
    trainer.train()

result = trainer.evaluate(tokenized_dataset['test'])
pprint.pprint(result)
