import os
import numpy as np
import pandas as pd

from sklearn.linear_model import LogisticRegressionCV
from sklearn.model_selection import StratifiedShuffleSplit, ShuffleSplit
from sklearn.multiclass import OneVsRestClassifier
from sklearn.metrics import f1_score

from skmultilearn.model_selection import IterativeStratification

from heat.utils import load_data, hyperboloid_to_klein, load_embedding, poincare_ball_to_hyperboloid

import functools
import fcntl
import argparse

def hamming_score(y_true, y_pred, normalize=True, sample_weight=None):
    '''
    Compute the Hamming score (a.k.a. label-based accuracy) for the multi-label case
    https://stackoverflow.com/q/32239577/395857
    '''
    acc_list = []
    for i in range(y_true.shape[0]):
        set_true = set( np.where(y_true[i])[0] )
        set_pred = set( np.where(y_pred[i])[0] )
        #print('\nset_true: {0}'.format(set_true))
        #print('set_pred: {0}'.format(set_pred))
        tmp_a = None
        if len(set_true) == 0 and len(set_pred) == 0:
            tmp_a = 1
        else:
            tmp_a = len(set_true.intersection(set_pred))/\
                    float( len(set_true.union(set_pred)) )
        #print('tmp_a: {0}'.format(tmp_a))
        acc_list.append(tmp_a)
    return np.mean(acc_list)

def evaluate_kfold_multilabel_classification(klein_embedding, labels, model,
	n_repeats=10):
	assert len(labels.shape) == 2
	sss = IterativeStratification(n_splits=n_repeats, 
		random_state=0,
		order=2)
	f1_micros = []
	f1_macros = []
	hammings = []
	i = 1
	for split_train, split_test in sss.split(klein_embedding, labels):
		model.fit(klein_embedding[split_train], labels[split_train])		
		predictions = model.predict(klein_embedding[split_test])
		f1_micro = f1_score(labels[split_test], predictions, average="micro")
		f1_macro = f1_score(labels[split_test], predictions, average="macro")
		f1_micros.append(f1_micro)
		f1_macros.append(f1_macro)
		print ("Done {}/{} folds".format(i, n_repeats))
		hamming = hamming_score(labels[split_test], predictions)
		hammings.append(hamming)
		i += 1
	return None, np.mean(f1_micros), np.mean(f1_macros), np.mean(hammings)

def evaluate_node_classification(klein_embedding, labels,
	label_percentages=np.arange(0.02, 0.11, 0.01), n_repeats=10):

	print ("Evaluating node classification")

	f1_micros = np.zeros((n_repeats, len(label_percentages)))
	f1_macros = np.zeros((n_repeats, len(label_percentages)))
	
	model = LogisticRegressionCV()

	if labels.shape[1] == 1:
		print ("single label clasification")
		labels = labels.flatten()

		split = StratifiedShuffleSplit

		for seed in range(n_repeats):
		
			for i, label_percentage in enumerate(label_percentages):

				sss = split(n_splits=1, test_size=1-label_percentage, random_state=seed)
				split_train, split_test = next(sss.split(klein_embedding, labels))
				model.fit(klein_embedding[split_train], labels[split_train])
				predictions = model.predict(klein_embedding[split_test])
				f1_micro = f1_score(labels[split_test], predictions, average="micro")
				f1_macro = f1_score(labels[split_test], predictions, average="macro")
				f1_micros[seed,i] = f1_micro
				f1_macros[seed,i] = f1_macro
			print ("completed repeat {}".format(seed+1))

	else: # multilabel classification
		print ("multilabel classification")
		model = OneVsRestClassifier(model)
		split = IterativeStratification

		for seed in range(n_repeats):
		
			for i, label_percentage in enumerate(label_percentages):

				sss = split(n_splits=2, order=3, random_state=seed,
					sample_distribution_per_fold=[1.0-label_percentage, label_percentage])
				split_train, split_test = next(sss.split(klein_embedding, labels))
				model.fit(klein_embedding[split_train], labels[split_train])
				predictions = model.predict(klein_embedding[split_test])
				f1_micro = f1_score(labels[split_test], predictions, average="micro")
				f1_macro = f1_score(labels[split_test], predictions, average="macro")
				f1_micros[seed,i] = f1_micro
				f1_macros[seed,i] = f1_macro
			print ("completed repeat {}".format(seed+1))

	return label_percentages, f1_micros.mean(axis=0), f1_macros.mean(axis=0)

def touch(path):
	with open(path, 'a'):
		os.utime(path, None)

def read_edgelist(fn):
	edges = []
	with open(fn, "r") as f:
		for line in (l.rstrip() for l in f.readlines()):
			edge = tuple(int(i) for i in line.split("\t"))
			edges.append(edge)
	return edges

def lock_method(lock_filename):
	''' Use an OS lock such that a method can only be called once at a time. '''

	def decorator(func):

		@functools.wraps(func)
		def lock_and_run_method(*args, **kwargs):

			# Hold program if it is already running 
			# Snippet based on
			# http://linux.byexamples.com/archives/494/how-can-i-avoid-running-a-python-script-multiple-times-implement-file-locking/
			fp = open(lock_filename, 'r+')
			done = False
			while not done:
				try:
					fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
					done = True
				except IOError:
					pass
			return func(*args, **kwargs)

		return lock_and_run_method

	return decorator 

def threadsafe_fn(lock_filename, fn, *args, **kwargs ):
	lock_method(lock_filename)(fn)(*args, **kwargs)

def save_test_results(filename, seed, data, ):
	d = pd.DataFrame(index=[seed], data=data)
	if os.path.exists(filename):
		test_df = pd.read_csv(filename, sep=",", index_col=0)
		test_df = d.combine_first(test_df)
	else:
		test_df = d
	test_df.to_csv(filename, sep=",")

def threadsafe_save_test_results(lock_filename, filename, seed, data):
	threadsafe_fn(lock_filename, save_test_results, filename=filename, seed=seed, data=data)

def parse_args():

	parser = argparse.ArgumentParser(description='Load Hyperboloid Embeddings and evaluate node classification')
	
	parser.add_argument("--edgelist", dest="edgelist", type=str, 
		help="edgelist to load.")
	parser.add_argument("--features", dest="features", type=str, 
		help="features to load.")
	parser.add_argument("--labels", dest="labels", type=str, 
		help="path to labels")

	parser.add_argument('--directed', action="store_true", help='flag to train on directed graph')


	parser.add_argument("--embedding", dest="embedding_filename",  
		help="path of embedding to load.")

	parser.add_argument("--test-results-dir", dest="test_results_dir",  
		help="path to save results.")

	parser.add_argument("--seed", type=int, default=0)

	parser.add_argument("--poincare", action="store_true")

	return parser.parse_args()

def main():

	args = parse_args()

	graph, features, node_labels = load_data(args)
	print ("Loaded dataset")

	embedding_df = load_embedding(args.embedding_filename)
	embedding = embedding_df.values

	if args.poincare:
		embedding = poincare_ball_to_hyperboloid(embedding)
	klein_embedding = hyperboloid_to_klein(embedding)

	label_percentages, f1_micros, f1_macros = evaluate_node_classification(klein_embedding, node_labels)

	test_results = {}
	for label_percentage, f1_micro, f1_macro in zip(label_percentages, f1_micros, f1_macros):
		print ("{:.2f}".format(label_percentage), "micro = {:.2f}".format(f1_micro), "macro = {:.2f}".format(f1_macro) )
		test_results.update({"{:.2f}_micro".format(label_percentage): f1_micro})
		test_results.update({"{:.2f}_macro".format(label_percentage): f1_macro})

	test_results_dir = args.test_results_dir
	if not os.path.exists(test_results_dir):
		os.makedirs(test_results_dir)
	test_results_filename = os.path.join(test_results_dir, "test_results.csv")
	test_results_lock_filename = os.path.join(test_results_dir, "test_results.lock")

	touch (test_results_lock_filename)

	print ("saving test results to {}".format(test_results_filename))
	threadsafe_save_test_results(test_results_lock_filename, test_results_filename, args.seed, data=test_results )

	
if __name__ == "__main__":
	main()