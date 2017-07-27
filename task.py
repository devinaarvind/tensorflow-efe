from model_param_space import ModelParamSpace
from hyperopt import fmin, tpe, hp, STATUS_OK, Trials, space_eval
from optparse import OptionParser
from utils import logging_utils
import numpy as np
import pandas as pd
from utils.data_utils import load_dict_from_txt
from utils.eval_utils import Scorer
import os
import config
import datetime
import tensorflow as tf
from efe import *

class AttrDict(dict):
	def __init__(self, *args, **kwargs):
		super(AttrDict, self).__init__(*args, **kwargs)
		self.__dict__ = self

class Task:
	def __init__(self, model_name, data_name, params_dict, logger):
		if data_name == "wn18":
			self.train_triples = pd.read_csv(config.WN18_TRAIN, names=["e1", "r", "e2"]).as_matrix()
			self.valid_triples = pd.read_csv(config.WN18_VALID, names=["e1", "r", "e2"]).as_matrix()
			self.test_triples = pd.read_csv(config.WN18_TEST, names=["e1", "r", "e2"]).as_matrix()
			self.e2id = load_dict_from_txt(config.WN18_E2ID)
			self.r2id = load_dict_from_txt(config.WN18_R2ID)
		else:
			self.train_triples = pd.read_csv(config.FB15K_TRAIN, names=["e1", "r", "e2"]).as_matrix()
			self.valid_triples = pd.read_csv(config.FB15K_VALID, names=["e1", "r", "e2"]).as_matrix()
			self.test_triples = pd.read_csv(config.FB15K_TEST, names=["e1", "r", "e2"]).as_matrix()
			self.e2id = load_dict_from_txt(config.FB15K_E2ID)
			self.r2id = load_dict_from_txt(config.FB15K_R2ID)

		self.model_name = model_name
		self.params_dict = params_dict
		self.hparams = AttrDict(params_dict)
		self.logger = logger
		self.n_entities = len(self.e2id)
		self.n_relations = len(self.r2id)
		self.scorer = Scorer(self.train_triples, self.valid_triples, self.test_triples, self.n_entities)

		self.model = self._get_model() 

	def _get_model(self):
		if "TransE_L2" in self.model_name:
			return TransE_L2(self.n_entities, self.n_relations, self.hparams)
	
	def _print_param_dict(self, d, prefix="      ", incr_prefix="      "):
		for k, v in sorted(d.items()):
			if isinstance(v, dict):
				self.logger.info("%s%s:" % (prefix, k))
				self.print_param_dict(v, prefix+incr_prefix, incr_prefix)
			else:
				self.logger.info("%s%s: %s" % (prefix, k, v))
	
	def create_session(self):
		session_conf = tf.ConfigProto(
				allow_soft_placement=True,
				log_device_placement=False)
		return tf.Session(config=session_conf)

	def cv(self):
		self.logger.info("=" * 50)
		self.logger.info("Params")
		self._print_param_dict(self.params_dict)
		self.logger.info("Results")
		self.logger.info("\t\tRun\t\tRaw MRR\t\tFiltered MRR")

		cv_res = []
		for i in range(5):
			sess = self.create_session()
			sess.run(tf.global_variables_initializer())
			self.model.fit(sess, self.train_triples)
			
			def pred_func(test_triples):
				return self.model.predict(sess, test_triples)

			res = self.scorer.compute_scores(pred_func, self.valid_triples)
			self.logger.info("\t\t%d\t\t%f\t\t%f" % (i, res.raw_mrr, res.mrr))
			cv_res.append(res)
			sess.close()

		self.raw_mrr = np.mean([res.raw_mrr for res in cv_res])
		self.mrr = np.mean([res.mrr for res in cv_res])

		self.raw_hits_at1 = np.mean([res.raw_hits_at1 for res in cv_res])
		self.raw_hits_at3 = np.mean([res.raw_hits_at3 for res in cv_res])
		self.raw_hits_at10 = np.mean([res.raw_hits_at10 for res in cv_res])

		self.hits_at1 = np.mean([res.hits_at1 for res in cv_res])
		self.hits_at3 = np.mean([res.hits_at3 for res in cv_res])
		self.hits_at10 = np.mean([res.hits_at10 for res in cv_res])

		self.logger.info("CV Raw MRR: %.6f" % self.raw_mrr)
		self.logger.info("CV Filtered MRR: %.6f" % self.mrr)
		self.logger.info("Raw: Hits@1 %.3f Hits@3 %.3f Hits@10 %.3f" % (self.raw_hits_at1, self.raw_hits_at3, self.raw_hits_at10))
		self.logger.info("Filtered: Hits@1 %.3f Hits@3 %.3f Hits@10 %.3f" % (self.hits_at1, self.hits_at3, self.hits_at10))
		self.logger.info("-" * 50)

class TaskOptimizer:
	def __init__(self, model_name, data_name, max_evals, logger):
		self.model_name = model_name
		self.data_name = data_name
		self.max_evals = max_evals
		self.logger = logger
		self.model_param_space = ModelParamSpace(self.model_name)

	def _obj(self, param_dict):
		param_dict = self.model_param_space._convert_into_param(param_dict)
		self.task = Task(self.model_name, self.data_name, param_dict, self.logger)
		self.task.cv()
		tf.reset_default_graph()
		ret = {
			"loss": -self.task.mrr,
			"attachments": {
				"raw_mrr": self.task.raw_mrr,
				"raw_hits_at1": self.task.raw_hits_at1,
				"raw_hits_at3": self.task.raw_hits_at3,
				"raw_hits_at10": self.task.raw_hits_at10,
				"hits_at1": self.task.hits_at1,
				"hits_at3": self.task.hits_at3,
				"hits_at10": self.task.hits_at10,
			},
			"status": STATUS_OK
		}
		return ret

	def run(self):
		trials = Trials()
		best = fmin(self._obj, self.model_param_space._build_space(), tpe.suggest, self.max_evals, trials)
		best_params = space_eval(self.model_param_space._build_space(), best)
		best_params = self.model_param_space._convert_into_param(best_params)
		trial_loss = np.asarray(trials.losses(), dtype=float)
		best_ind = np.argmin(trial_loss)
		mrr = -trial_loss[best_ind]
		raw_mrr = trials.trial_attachments(trials.trials[best_ind])["raw_mrr"]
		raw_hits_at1 = trials.trial_attachments(trials.trials[best_ind])["raw_hits_at1"]
		raw_hits_at3 = trials.trial_attachments(trials.trials[best_ind])["raw_hits_at3"]
		raw_hits_at10 = trials.trial_attachments(trials.trials[best_ind])["raw_hits_at10"]
		hits_at1 = trials.trial_attachments(trials.trials[best_ind])["hits_at1"]
		hits_at3 = trials.trial_attachments(trials.trials[best_ind])["hits_at3"]
		hits_at10 = trials.trial_attachments(trials.trials[best_ind])["hits_at10"]
		self.logger.info("-"*50)
		self.logger.info("Best CV Results:")
		self.logger.info("Raw MRR: %.6f" % raw_mrr)
		self.logger.info("Filtered MRR: %.6f" % mrr)
		self.logger.info("Raw: Hits@1 %.3f Hits@3 %.3f Hits@10 %.3f" % (raw_hits_at1, raw_hits_at3, raw_hits_at10))
		self.logger.info("Filtered: Hits@1 %.3f Hits@3 %.3f Hits@10 %.3f" % (hits_at1, hits_at3, hits_at10))
		self.logger.info("Best Param:")
		self.task._print_param_dict(best_params)
		self.logger.info("-"*50)

def parse_args(parser):
	parser.add_option("-m", "--model", type="string", dest="model_name", default="TransE_L2")
	parser.add_option("-d", "--data", type="string", dest="data_name", default="wn18")
	parser.add_option("-e", "--eval", type="int", dest="max_evals", default=100)
	options, args = parser.parse_args()
	return options, args

def main(options):
	time_str = datetime.datetime.now().isoformat()
	logname = "[Model@%s]_[Data@%s]_%s.log" % (
			options.model_name, options.data_name, time_str)
	logger = logging_utils._get_logger(config.LOG_PATH, logname)
	optimizer = TaskOptimizer(options.model_name, options.data_name, options.max_evals, logger)
	optimizer.run()

if __name__ == "__main__":
	parser = OptionParser()
	options, args = parse_args(parser)
	main(options)
