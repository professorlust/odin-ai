from __future__ import print_function, division, absolute_import

import os
import shutil
import pickle
import warnings
from enum import Enum
from collections import defaultdict, OrderedDict

import numpy as np
import numba as nb
from scipy.io import wavfile

from odin import visual as V
from odin.preprocessing.signal import anything2wav
from odin.utils import (Progbar, get_exppath, cache_disk, ctext,
                        mpi, args_parse, select_path, get_logpath,
                        get_script_name, get_script_path, get_module_from_path,
                        catch_warnings_error, catch_warnings_ignore)
from odin.stats import freqcount, sampling_iter
from odin import fuel as F

# ===========================================================================
# Configuration
# ===========================================================================
class Config(object):
  # ====== Acoustic features ====== #
  FRAME_LENGTH = 0.025
  STEP_LENGTH = 0.01
  SAMPLE_RATE = 8000
  WINDOW = 'hamm'
  NFFT = 512
  # Random seed for reproducibility
  SUPER_SEED = 52181208
  # for training
  MINIMUM_UTT_DURATION = 1. # in seconds
  MINIMUM_UTT_PER_SPEAKERS = 8 # number of utterances

class SystemStates(Enum):
  """ SystemStates """
  UNKNOWN = 0
  EXTRACT_FEATURES = 1
  TRAINING = 2
  SCORING = 3

# ===========================================================================
# General arguments for all experiments
# ===========================================================================
_args = args_parse(descriptions=[
    ('recipe', 'recipe is the name of acoustic Dataset defined in feature_recipes.py', None),
    ('-feat', 'specific name for the acoustic features, extracted from the given recipe', None, ''),
    ('-aug', 'augmentation dataset: musan, rirs; could be multiple dataset '
             'for training: "musan,rirs"', None, 'None'),
    ('-ncpu', 'number of CPU to be used, if <= 0, auto-select', None, 0),
    # for scoring
    ('-sys', 'name of the system for scoring: xvec, ivec, e2e ...', None, 'xvec'),
    ('-sysid', 'when a system is saved multiple checkpoint (e.g. sys.0.ai)', None, '-1'),
    ('-score', 'name of dataset for scoring, multiple dataset split by ","', None, 'sre18dev,sre18eval'),
    ('-backend', 'list of dataset for training the backend: '
                 'PLDA, SVM or Cosine', None, 'sre04,sre05,sre06,sre08,sre10'),
    ('-lda', 'if > 0, running LDA before training the backend '
             'with given number of components', None, 0),
    ('-plda', 'number of PLDA components, must be > 0 ', None, 150),
    ('--mll', 'pre-fitting maximum likelihood before training PLDA', None, False),
    ('--showllk', 'show LLK during training of PLDA, this will slow thing down', None, False),
    # for training
    ('-downsample', 'absolute number of files used for training', None, 0),
    ('-exclude', 'list of excluded dataset not for training, multiple dataset split by ","', None, ''),
    # for ivector
    ('-nmix', 'for i-vector training, number of Gaussian components', None, 2048),
    ('-tdim', 'for i-vector training, number of latent dimension for i-vector', None, 600),
    # for DNN
    ('-utt', 'for x-vector training, maximum utterance length', None, 4),
    ('-batch', 'batch size, for training DNN', None, 64),
    ('-epoch', 'number of epoch, for training DNN', None, 12),
    ('-clip', 'The maximum change in parameters allowed per minibatch, '
              'measured in Euclidean norm over the entire model (change '
              'will be clipped to this value), kaldi use 2.0', None, 2.0),
    ('-lr', 'learning rate for Adam, kaldi use 0.001 by default, we use 0.0001', None, 0.001),
    # others
    ('--override', 'override previous experiments', None, False),
    ('--debug', 'enable debugging', None, False),
])
IS_DEBUGGING = bool(_args.debug)
IS_OVERRIDE = bool(_args.override)
# this variable determine which state is running
CURRENT_STATE = SystemStates.UNKNOWN
# ====== Features extraction ====== #
FEATURE_RECIPE = str(_args.recipe)
FEATURE_NAME = FEATURE_RECIPE.split('_')[0] if len(str(_args.feat)) == 0 else str(_args.feat)
AUGMENTATION_NAME = _args.aug
TRAINING_DATASET = ['mx6', 'voxceleb1', 'voxceleb2', 'swb', 'fisher',
                    'sre04', 'sre05', 'sre06', 'sre08', 'sre10']
# ====== DNN ====== #
BATCH_SIZE = int(_args.batch)
EPOCH = int(_args.epoch)
LEARNING_RATE = float(_args.lr)
GRADIENT_CLIPPING = float(_args.clip)
# ====== searching for the appropriate system ====== #
SCORE_SYSTEM_NAME = _args.sys
SCORE_SYSTEM_ID = int(_args.sysid)
N_LDA = int(_args.lda)
N_PLDA = int(_args.plda)
assert N_PLDA > 0, "Number of PLDA components must > 0, but given: %d" % N_PLDA
PLDA_MAXIMUM_LIKELIHOOD = bool(_args.mll)
PLDA_SHOW_LLK = bool(_args.showllk)
# ====== system ====== #
NCPU = min(18, mpi.cpu_count() - 2) if _args.ncpu <= 0 else int(_args.ncpu)
# ====== helper for checking the requirement ====== #
def _check_feature_extraction_requirement():
  # check requirement for feature extraction
  from shutil import which
  if which('sox') is None:
    raise RuntimeError("`sox` was not installed")
  if which('sph2pipe') is None:
    raise RuntimeError("`sph2pipe` was not installed")
  if which('ffmpeg') is None:
    raise RuntimeError("`ffmpeg` was not installed")

def _check_recipe_name_for_extraction():
  # check the requirement of recipe name for feature extraction
  if '_' in FEATURE_RECIPE:
    raise ValueError("'_' can appear in recipe name which is: '%s'" % FEATURE_RECIPE)
# ====== check the running script to determine the current running states ====== #
_script_name = get_script_name()
if _script_name in ('speech_augmentation', 'speech_features_extraction'):
  CURRENT_STATE = SystemStates.EXTRACT_FEATURES
  _check_feature_extraction_requirement()
  _check_recipe_name_for_extraction()
elif _script_name in ('train_xvec', 'train_ivec', 'train_tvec'):
  CURRENT_STATE = SystemStates.TRAINING
elif _script_name in ('make_score'):
  CURRENT_STATE = SystemStates.SCORING
  _check_feature_extraction_requirement()
else:
  raise RuntimeError("Unknown states for current running script: %s/%s" %
    (get_script_path(), get_script_name()))
# some fancy log of current state
print(ctext('====================================', 'red'))
print(ctext("System state:", 'cyan'), ctext(CURRENT_STATE, 'yellow'))
print(ctext('====================================', 'red'))
# ===========================================================================
# FILE LIST PATH
# ===========================================================================
# ====== basic directories ====== #
EXP_DIR = get_exppath('sre', override=False)
# this folder store extracted vectors for training backend and extracting scores
VECTORS_DIR = os.path.join(EXP_DIR, 'vectors')
if not os.path.exists(VECTORS_DIR):
  os.mkdir(VECTORS_DIR)
# this folder store the results
RESULT_DIR = os.path.join(EXP_DIR, 'results')
if not os.path.exists(RESULT_DIR):
  os.mkdir(RESULT_DIR)
# ====== raw data ====== #
PATH_BASE = select_path(
    '/media/data2/SRE_DATA',
    '/mnt/sdb1/SRE_DATA',
default='')
# path to directory contain following folders:
##############
#   * fisher
#   * mx6
#   * sre04
#   * sre05
#   * sre06
#   * sre08
#   * sre10
#   * swb
#   * voxceleb1
#   * voxceleb2
###############
#   * musan
#   * rirs
###############
#   * sre18dev
#   * sre18eval
PATH_RAW_DATA = {
    'mx6': PATH_BASE,
    'voxceleb1': PATH_BASE,
    'voxceleb2': PATH_BASE,
    'swb': PATH_BASE,
    'fisher': PATH_BASE,
    'sre04': os.path.join(PATH_BASE, 'NIST1996_2008/SRE02_SRE06'),
    'sre05': os.path.join(PATH_BASE, 'NIST1996_2008/SRE96_SRE05'),
    'sre06': os.path.join(PATH_BASE, 'NIST1996_2008/SRE02_SRE06'),
    'sre08': PATH_BASE,
    'sre10': PATH_BASE,
    'sre18dev': PATH_BASE,
    'sre18eval': PATH_BASE,
    # noise datasets
    'musan': PATH_BASE,
    'rirs': PATH_BASE,
}
# all features will be stored here
OUTPUT_DIR = select_path(
    '/home/trung/data',
    '/media/data1',
    '/mnt/sda1'
)
PATH_ACOUSTIC_FEATURES = os.path.join(OUTPUT_DIR, "SRE_FEAT")
if not os.path.exists(PATH_ACOUSTIC_FEATURES):
  os.mkdir(PATH_ACOUSTIC_FEATURES)
# ===========================================================================
# Load the file list
# ===========================================================================
sre_file_list = F.load_sre_list()
print('README at:', ctext(sre_file_list['README.txt'], 'cyan'))
sre_file_list = {k: v
                 for k, v in sre_file_list.items()
                 if isinstance(v, np.ndarray)}
print("Original dataset:")
for k, v in sorted(sre_file_list.items(), key=lambda x: x[0]):
  print(' ', ctext('%-18s' % k, 'yellow'), ':',
    ctext(v.shape, 'cyan'))
# ===========================================================================
# Validate scoring dataset
# ===========================================================================
def validate_scoring_dataset(in_path_raw, score_dataset):
  all_files = {}
  for dsname in score_dataset:
    if dsname not in sre_file_list:
      raise ValueError("Cannot find dataset with name: '%s' in the file list" % dsname)
    if dsname not in in_path_raw:
      raise ValueError("Cannot find dataset with name: '%s' in provided path" % dsname)
    base_path = in_path_raw[dsname]

    ds = []
    for row in sre_file_list[dsname]:
      path = os.path.join(base_path, row[0])
      # every file must exist
      if not os.path.exists(path):
        raise RuntimeError("File not exist at path: %s" % path)
      ds.append([path] + row[1:4].tolist() + [dsname])
    all_files[dsname] = np.array(ds)
  # Header:
  #  0       1      2        3           4
  # path, channel, name, something, dataset_name
  return all_files

# ====== check dataset for scoring ====== #
if CURRENT_STATE == SystemStates.SCORING:
  assert len(_args.score) > 0, \
  "No dataset are provided for scoring, specify '-score' option"

  # for scoring
  SCORING_DATASETS = validate_scoring_dataset(
      in_path_raw=PATH_RAW_DATA,
      score_dataset=str(_args.score).strip().split(','))
  print("Processed scoring dataset:")
  for dsname, dsarray in sorted(SCORING_DATASETS.items(),
                                key=lambda x: x[0]):
    print('  ', ctext('%-10s' % dsname, 'yellow'), ':',
          '%s' % ctext(dsarray.shape, 'cyan'))

  # for training the backend
  BACKEND_DATASETS = validate_scoring_dataset(
      in_path_raw=PATH_RAW_DATA,
      score_dataset=str(_args.backend).strip().split(','))
  assert len(BACKEND_DATASETS) > 0, \
  "Datasets for training the backend must be provided"
  print("Processed backend dataset:")
  for dsname, dsarray in sorted(BACKEND_DATASETS.items(),
                                key=lambda x: x[0]):
    print('  ', ctext('%-10s' % dsname, 'yellow'), ':',
          '%s' % ctext(dsarray.shape, 'cyan'))
# ===========================================================================
# Validating the Noise dataset for augmentation
# ===========================================================================
@cache_disk
def validating_noise_data(in_path_raw):
  # preparing
  noise_dataset = ['musan', 'rirs']
  all_files = defaultdict(list)
  n_files = sum(len(sre_file_list[i])
                for i in noise_dataset
                if i in sre_file_list)
  n_non_exist = 0
  n_exist = 0
  prog = Progbar(target=n_files, print_summary=True,
                 name="Validating noise dataset")
  prog.set_summarizer(key='#Non-exist', fn=lambda x: x[-1])
  prog.set_summarizer(key='#Exist', fn=lambda x: x[-1])
  # check all dataset
  for ds_name in noise_dataset:
    if ds_name not in sre_file_list:
      continue
    if ds_name not in in_path_raw:
      continue
    base_path = in_path_raw[ds_name]
    base_ds = all_files[ds_name]
    # start validating
    for row in sre_file_list[ds_name]:
      # check file
      path, channel, name, noise_type, duration = row[:5]
      path = os.path.join(base_path, path)
      if os.path.exists(path):
        base_ds.append([path, channel, name, noise_type, duration])
        n_exist += 1
      else:
        n_non_exist += 1
      # update progress
      prog['ds'] = ds_name
      prog['#Exist'] = n_exist
      prog['#Non-exist'] = n_non_exist
      prog.add(1)
  # ====== return ====== #
  # Header:
  #  0       1      2         3         4
  # path, channel, name, noise_type, duration
  return {key: np.array(sorted(val, key=lambda x: x[0]))
          for key, val in all_files.items()}
# ==================== run the validation ==================== #
if CURRENT_STATE == SystemStates.EXTRACT_FEATURES:
  ALL_NOISE = validating_noise_data(
      in_path_raw=PATH_RAW_DATA)
  print("Processed noise data:")
  for ds_name, noise_list in ALL_NOISE.items():
    print(" ", ctext(ds_name, 'yellow'), ':', noise_list.shape)
    if len(noise_list) == 0:
      continue
    for name, count in sorted(freqcount(noise_list[:, 3]).items(),
                              key=lambda x: x[0]):
      print('  ', ctext('%-10s' % name, 'yellow'), ':',
            '%s(files)' % ctext('%-6d' % count, 'cyan'))
# ===========================================================================
# Validating the file list of training data
# ===========================================================================
@cache_disk
def validating_training_data(in_path_raw, training_dataset):
  file_list = {ds: sre_file_list[ds]
               for ds in training_dataset
               if ds in sre_file_list}
  # ====== meta info ====== #
  all_files = []
  non_exist_files = []
  extension_count = defaultdict(int)
  total_data = sum(v.shape[0]
                   for k, v in file_list.items()
                   if k not in('musan', 'rirs'))
  # ====== progress ====== #
  prog = Progbar(target=total_data,
                 print_summary=True, print_report=True,
                 name="Preprocessing File List")
  prog.set_summarizer('#Files', fn=lambda x: x[-1])
  prog.set_summarizer('#Non-exist', fn=lambda x: x[-1])
  # ====== iterating ====== #
  for ds_name, data in sorted(file_list.items(),
                              key=lambda x: x[0]):
    if ds_name in ('musan', 'rirs'):
      continue
    for row in data:
      path, channel, name, spkid = row[:4]
      assert channel in ('0', '1')
      # check path provided
      if ds_name in in_path_raw:
        path = os.path.join(in_path_raw[ds_name], path)
      # create new row
      start_time = '-'
      end_time = '-'
      if ds_name == 'mx6':
        start_time, end_time = row[-2:]
      new_row = [path, channel, name,
                 ds_name + '_' + spkid, ds_name,
                 start_time, end_time]
      # check file exist
      if os.path.exists(path):
        all_files.append(new_row)
      else:
        non_exist_files.append(new_row)
      # extension
      ext = os.path.splitext(path)[-1]
      extension_count[ext + '-' + ds_name] += 1
      # update progress
      prog['Dataset'] = ds_name
      prog['#Files'] = len(all_files)
      prog['#Non-exist'] = len(non_exist_files)
      prog.add(1)
  # final results
  all_files = np.array(all_files)
  if len(all_files) == 0:
    return all_files, np.array(non_exist_files), extension_count
  # ====== check no duplicated name ====== #
  n_files = len(all_files)
  n_unique_files = len(np.unique(all_files[:, 2]))
  assert n_files == n_unique_files, \
  'Found duplicated name: %d != %d' % (n_files, n_unique_files)
  # ====== check no duplicated speaker ====== #
  n_spk = sum(len(np.unique(dat[:, 3]))
              for name, dat in file_list.items()
              if name not in ('musan', 'rirs'))
  n_unique_spk = len(np.unique(all_files[:, 3]))
  assert n_spk == n_unique_spk, \
  'Found duplicated speakers: %d != %d' % (n_spk, n_unique_spk)
  # ====== return ====== #
  # Header:
  #  0       1      2      3       4          5         6
  # path, channel, name, spkid, dataset, start_time, end_time
  return all_files, np.array(non_exist_files), extension_count
# ==================== run the validation process ==================== #
if CURRENT_STATE == SystemStates.EXTRACT_FEATURES:
  (ALL_FILES, NON_EXIST_FILES, ext_count) = validating_training_data(
      in_path_raw=PATH_RAW_DATA,
      training_dataset=TRAINING_DATASET
  )
  if len(ALL_FILES) == 0:
    raise RuntimeError("No files found for feature extraction")

  # list of all dataset
  ALL_DATASET = sorted(np.unique(ALL_FILES[:, 4]))
  print("All extensions:")
  for name, val in sorted(ext_count.items(), key=lambda x: x[0]):
    print('  ', '%-16s' % name, ':', ctext('%-6d' % val, 'cyan'), '(files)')
  print("#Speakers:", ctext(len(np.unique(ALL_FILES[:, 3])), 'cyan'))

  # map Dataset_name -> speaker_ID
  DS_SPK = defaultdict(list)
  for row in ALL_FILES:
    DS_SPK[row[4]].append(row[3])
  DS_SPK = {k: sorted(set(v))
            for k, v in DS_SPK.items()}

  print("Processed datasets:")
  for name, count in sorted(freqcount(ALL_FILES[:, 4]).items(),
                            key=lambda x: x[0]):
    print('  ', ctext('%-10s' % name, 'yellow'), ':',
          '%s(files)' % ctext('%-6d' % count, 'cyan'),
          '%s(spk)' % ctext('%-4d' % len(DS_SPK[name]), 'cyan'))
# ===========================================================================
# PATH HELPER
# ===========================================================================
def get_model_path(system_name, logging=True):
  """
  Parameters
  ----------
  args_name : list of string
    list of name for parsed argument, taken into account for creating
    model name

  Return
  ------
  exp_dir, model_path, log_path
  """
  if system_name == 'xvec':
    args_name = ['utt']
  elif system_name == 'ivec':
    args_name = ['nmix', 'tdim']
  else:
    raise ValueError("No support for system with name: %s" % system_name)
  # ====== prefix ====== #
  name = '_'.join([str(system_name).lower(),
                   FEATURE_RECIPE.replace('_', ''),
                   FEATURE_NAME])
  # ====== concat the attributes ====== #
  for i in sorted(str(i) for i in args_name):
    name += '_' + str(int(getattr(_args, i)))
  # ====== check the exclude dataset ====== #
  excluded_dataset = str(_args.exclude).strip()
  if len(excluded_dataset) > 0:
    for excluded in sorted(set(excluded_dataset.split(','))):
      assert excluded in sre_file_list or excluded == 'noise', \
      "Unknown excluded dataset with name: '%s'" % excluded
      name += '_' + excluded
  # ====== check save_path ====== #
  save_path = os.path.join(EXP_DIR, name)
  if os.path.exists(save_path) and IS_OVERRIDE:
    print("Override path:", ctext(save_path, 'yellow'))
    shutil.rmtree(save_path)
  if not os.path.exists(save_path):
    os.mkdir(save_path)
  # ====== return path ====== #
  log_path = get_logpath(name='log.txt', increasing=True,
                         odin_base=False, root=save_path)
  model_path = os.path.join(save_path, 'model.ai')
  if bool(logging):
    print("Model path:", ctext(model_path, 'cyan'))
    print("Log path:", ctext(log_path, 'cyan'))
  return save_path, model_path, log_path
# ===========================================================================
# Data helper
# ===========================================================================
def prepare_dnn_feeder_recipe(name2label=None, n_speakers=None):
  frame_length = float(_args.utt) / Config.STEP_LENGTH
  recipes = [
      F.recipes.Sequencing(frame_length=frame_length,
                           step_length=frame_length,
                           end='pad', pad_value=0, pad_mode='post',
                           data_idx=0),
  ]
  if name2label is not None and n_speakers is not None:
    recipes += [
        F.recipes.Name2Label(lambda name:name2label[name],
                             ref_idx=0),
        F.recipes.LabelOneHot(nb_classes=n_speakers, data_idx=1)
    ]
  elif (name2label is not None and n_speakers is None) or\
  (name2label is None and n_speakers is not None):
    raise RuntimeError("name2label and n_speakers must both be None, or not-None")
  return recipes

def filter_utterances(X, indices):
  minimum_amount_of_frames = Config.MINIMUM_UTT_DURATION / Config.STEP_LENGTH

  prog = Progbar(target=len(indices),
                 print_report=True, print_summary=True,
                 name='Filtering broken utterances')
  prog.set_summarizer('zero-length', fn=lambda x: x[-1])
  prog.set_summarizer('min-frames', fn=lambda x: x[-1])
  prog.set_summarizer('zero-var', fn=lambda x: x[-1])
  prog.set_summarizer('small-var', fn=lambda x: x[-1])
  prog.set_summarizer('overflow', fn=lambda x: x[-1])

  # ====== mpi function for checking ====== #
  @nb.jit(nopython=True, nogil=True)
  def _fast_mean_var_ax0(z):
    # using this function for calculating mean and variance
    # can double the speed but cannot check overflow,
    # only accept float32 or float64 input
    s1 = np.zeros(shape=(z.shape[1],), dtype=z.dtype)
    s2 = np.zeros(shape=(z.shape[1],), dtype=z.dtype)
    for i in range(z.shape[0]):
      s1 += z[i]
      s2 += np.power(z[i], 2)
    mean = s1 / z.shape[0]
    var = s2 / z.shape[0] - np.power(mean, 2)
    return mean, var

  def _mpi_func(jobs):
    for name, (start, end) in jobs:
      y = X[start:end]
      # flags
      is_zero_len = False
      is_zero_var = False
      is_small_var = False
      is_min_frames = False
      is_overflow = False
      # checking length
      if y.shape[0] == 0:
        is_zero_len = True
      elif y.shape[0] <= minimum_amount_of_frames:
        is_min_frames = True
      # checking statistics
      else:
        with catch_warnings_error(RuntimeWarning):
          try:
            # mean = np.mean(y, axis=-1)
            var = np.var(y, axis=-1)
            # min_val = np.min(y, axis=-1)
            # max_val = np.max(y, axis=-1)
          # numerical unstable
          except RuntimeWarning as w:
            if 'overflow encountered' in str(w):
              is_overflow = True
            else:
              print(name, ':', w)
          # process with more numerical filtering
          else:
            if np.any(np.isclose(var, 0)):
              is_zero_var = True
            # very heuristic and aggressive here
            # filter-out anything with ~16.67% of low-var
            # this could remove 1/3 of the original data
            if np.sum(var < 0.01) > (len(y) / 6):
              is_small_var = True
      # return the flags
      yield (name, is_zero_len, is_min_frames,
             is_zero_var, is_small_var,
             is_overflow)
  # ====== running the multiprocessing filter ====== #
  zero_len_files = {}
  min_frame_files = {}
  zero_var_files = {}
  small_var_files = {}
  overflow_files = {}
  for res in mpi.MPI(jobs=sorted(indices.items(),
                                 key=lambda x: x[1][0]),
                     func=_mpi_func,
                     ncpu=NCPU, batch=250):
    name = res[0]
    if res[1]:
      zero_len_files[name] = 1
    if res[2]:
      min_frame_files[name] = 1
    if res[3]:
      zero_var_files[name] = 1
    if res[4]:
      small_var_files[name] = 1
    if res[5]:
      overflow_files[name] = 1
    # update progress
    prog['name'] = name[:48]
    prog['zero-length'] = len(zero_len_files)
    prog['min-frames'] = len(min_frame_files)
    prog['zero-var'] = len(zero_var_files)
    prog['small-var'] = len(small_var_files)
    prog['overflow'] = len(overflow_files)
    prog.add(1)
  # ====== remove broken files ====== #
  new_indices = {name: (start, end)
                 for name, (start, end) in indices.items()
                 if name not in zero_len_files and
                 name not in min_frame_files and
                 name not in zero_var_files and
                 name not in small_var_files and
                 name not in overflow_files}
  print("Filtered #utterances: %s/%s (files)" %
    (ctext(len(indices) - len(new_indices), 'lightcyan'),
     ctext(len(indices), 'cyan')))
  return new_indices

def prepare_dnn_data():
  assert int(_args.utt) > Config.MINIMUM_UTT_DURATION, \
      "Training utterances length is: %d(s), must be greater than minimum utterance duration: %d(s)" % \
      (int(_args.utt), Config.MINIMUM_UTT_DURATION)
  # ====== prepare the dataset ====== #
  path = os.path.join(PATH_ACOUSTIC_FEATURES, FEATURE_RECIPE)
  assert os.path.exists(path), "Cannot find acoustic dataset at path: %s" % path
  ds = F.Dataset(path=path, read_only=True)
  rand = np.random.RandomState(seed=Config.SUPER_SEED)
  # ====== find the right feature ====== #
  ids_name = 'indices_%s' % FEATURE_NAME
  assert FEATURE_NAME in ds, "Cannot find feature with name: %s" % FEATURE_NAME
  assert ids_name in ds, "Cannot find indices with name: %s" % ids_name
  X = ds[FEATURE_NAME]
  # ====== exclude some dataset ====== #
  if len(_args.exclude) > 0:
    exclude_dataset = {i: 1 for i in str(_args.exclude.strip()).split(',')}
    print("* Excluded dataset:", ctext(exclude_dataset, 'cyan'))
    indices = {name: (start, end)
               for name, (start, end) in ds[ids_name].items()
               if ds['dsname'][name] not in exclude_dataset}
    # special case exclude all the noise data
    if 'noise' in exclude_dataset:
      indices = {name: (start, end)
                 for name, (start, end) in indices.items()
                 if '/' not in name}
  else:
    indices = {i: j for i, j in ds[ids_name].items()}
  # ====== down-sampling if necessary ====== #
  if _args.downsample > 1000:
    dataset2name = defaultdict(list)
    # ordering the indices so we sample the same set every time
    for name in sorted(indices.keys()):
      dataset2name[ds['dsname'][name]].append(name)
    n_total_files = len(indices)
    n_sample_files = int(_args.downsample)
    # get the percentage of each dataset
    dataset2per = {i: len(j) / n_total_files
                   for i, j in dataset2name.items()}
    # sampling based on percentage
    _ = {}
    for dsname, flist in dataset2name.items():
      rand.shuffle(flist)
      n_dataset_files = int(dataset2per[dsname] * n_sample_files)
      _.update({i: indices[i]
                for i in flist[:n_dataset_files]})
    indices = _
  # ====== * filter out "bad" sample ====== #
  indices = filter_utterances(X=X, indices=indices)
  # ====== filter-out by number of utt-per-speaker ====== #
  spk2utt = defaultdict(list)
  for name in indices.keys():
    spk2utt[ds['spkid'][name]].append(name)

  n_utt_removed = 0
  n_spk_removed = 0
  keep_utt = {}
  for spk, utt in spk2utt.items():
    if len(utt) < Config.MINIMUM_UTT_PER_SPEAKERS:
      n_utt_removed += len(utt)
      n_spk_removed += 1
    else:
      for u in utt:
        keep_utt[u] = 1

  print("Removed min-utt/spk:  %s/%s(utt)  %s/%s(spk)" % (
      ctext(n_utt_removed, 'lightcyan'), ctext(len(indices), 'cyan'),
      ctext(n_spk_removed, 'lightcyan'), ctext(len(spk2utt), 'cyan')
  ))
  assert len(indices) == n_utt_removed + len(keep_utt), "Not possible!"
  indices = {name: (start, end)
             for name, (start, end) in indices.items()
             if name in keep_utt}
  # ====== all training file name ====== #
  # modify here to train full dataset
  all_name = sorted(indices.keys())
  rand.shuffle(all_name); rand.shuffle(all_name)
  n_files = len(all_name)
  print("#Files:", ctext(n_files, 'cyan'))
  # ====== speaker mapping ====== #
  name2spk = {name: ds['spkid'][name]
              for name in all_name}
  all_speakers = sorted(set(name2spk.values()))
  spk2label = {spk: i
               for i, spk in enumerate(all_speakers)}
  name2label = {name: spk2label[spk]
                for name, spk in name2spk.items()}
  assert len(name2label) == len(all_name)
  print("#Speakers:", ctext(len(all_speakers), 'cyan'))
  # ====== stratify sampling based on speaker ====== #
  valid_name = []
  # create speakers' cluster
  label2name = defaultdict(list)
  for name, label in name2label.items():
    label2name[label].append(name)
  # for each speaker with >= 3 utterance
  for label, name_list in label2name.items():
    if len(name_list) < 3:
      continue
    n = max(1, int(0.1 * len(name_list))) # 10% for validation
    valid_name += rand.choice(a=name_list, size=n).tolist()
  # train list is the rest
  _ = {name: 1 for name in valid_name}
  train_name = [i for i in all_name
                if i not in _]
  # ====== split training and validation ====== #
  train_indices = {name: indices[name]
                   for name in train_name}
  valid_indices = {name: indices[name]
                   for name in valid_name}

  print("#Train files:", ctext('%-8d' % len(train_indices), 'cyan'),
        "#spk:", ctext(len(set(name2label[name]
                               for name in train_name)), 'cyan'),
        "#noise:", ctext(len([name for name in train_name
                              if '/' in name]), 'cyan'))

  print("#Valid files:", ctext('%-8d' % len(valid_indices), 'cyan'),
        "#spk:", ctext(len(set(name2label[name]
                               for name in valid_name)), 'cyan'),
        "#noise:", ctext(len([name for name in valid_name
                              if '/' in name]), 'cyan'))
  # ====== create the recipe ====== #
  assert all(name in name2label
             for name in train_indices.keys())
  assert all(name in name2label
            for name in valid_indices.keys())
  recipes = prepare_dnn_feeder_recipe(name2label=name2label,
                                      n_speakers=len(all_speakers))
  train_feeder = F.Feeder(
      data_desc=F.IndexedData(data=X,
                              indices=train_indices),
      batch_mode='batch', ncpu=NCPU, buffer_size=256)
  valid_feeder = F.Feeder(
      data_desc=F.IndexedData(data=X,
                              indices=valid_indices),
      batch_mode='batch', ncpu=max(2, NCPU // 4), buffer_size=64)
  train_feeder.set_recipes(recipes)
  valid_feeder.set_recipes(recipes)
  print(train_feeder)
  print(valid_feeder)
  # ====== debugging ====== #
  if IS_DEBUGGING:
    import matplotlib
    matplotlib.use('Agg')
    prog = Progbar(target=len(valid_feeder), print_summary=True,
                   name="Iterating validation set")
    samples = []
    n_visual = 250
    for name, idx, X, y in valid_feeder.set_batch(batch_size=100000,
                                                  batch_mode='file',
                                                  seed=None, shuffle_level=0):
      assert idx == 0, "Utterances longer than %.2f(sec)" % (100000 * Config.STEP_LENGTH)
      prog['X'] = X.shape
      prog['y'] = y.shape
      prog.add(X.shape[0])
      # random sampling
      if rand.rand(1) < 0.5 and len(samples) < n_visual:
        for i in rand.randint(0, X.shape[0], size=4, dtype='int32'):
          samples.append((name, X[i], np.argmax(y[i], axis=-1)))
    # plot the spectrogram
    n_visual = len(samples)
    V.plot_figure(nrow=n_visual, ncol=8)
    for i, (name, X, y) in enumerate(samples):
      is_noise = '/' in name
      assert name2label[name] == y, "Speaker lable mismatch for file: %s" % name
      name = name.split('/')[0]
      dsname = ds['dsname'][name]
      spkid = ds['spkid'][name]
      y = np.argmax(y, axis=-1)
      ax = V.plot_spectrogram(X.T,
                              ax=(n_visual, 1, i + 1),
                              title='#%d' % (i + 1))
      ax.set_title('[%s][%s]%s  %s' %
                   ('noise' if is_noise else 'clean', dsname, name, spkid),
                   fontsize=6)
    # don't need to be high resolutions
    V.plot_save('/tmp/tmp.pdf', dpi=12)
    exit()
  # ====== return ====== #
  exit()
  return train_feeder, valid_feeder, all_speakers
# ===========================================================================
# Evaluation and validation helper
# ===========================================================================
def validate_features_dataset(output_dataset_path, ds_validation_path):
  ds = F.Dataset(output_dataset_path, read_only=True)
  print(ds)

  features = {}
  for key, val in ds.items():
    if 'indices_' in key:
      name = key.split('_')[-1]
      features[name] = (val, ds[name])

  all_indices = [val[0] for val in features.values()]
  # ====== sampling 250 files ====== #
  all_files = sampling_iter(it=all_indices[0].keys(), k=250,
                            seed=Config.SUPER_SEED)
  all_files = [f for f in all_files
               if all(f in ids for ids in all_indices)]
  print("#Samples:", ctext(len(all_files), 'cyan'))

  # ====== ignore the 20-figures warning ====== #
  with catch_warnings_ignore(RuntimeWarning):
    for file_name in all_files:
      X = {}
      for feat_name, (ids, data) in features.items():
        start, end = ids[file_name]
        X[feat_name] = data[start:end][:].astype('float32')
      V.plot_multiple_features(features=X, fig_width=20,
            title='[%s]%s' % (ds['dsname'][file_name], file_name))

  V.plot_save(ds_validation_path, dpi=12)
