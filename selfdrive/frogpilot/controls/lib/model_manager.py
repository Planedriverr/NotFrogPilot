import json
import os
import re
import requests
import shutil
import time
import urllib.request

from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params

from openpilot.selfdrive.frogpilot.controls.lib.download_functions import GITHUB_URL, GITLAB_URL, get_remote_file_size, get_repository_url, verify_download
from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_functions import MODELS_PATH, delete_file

VERSION = "v5"

DEFAULT_MODEL = "north-dakota-v2"
DEFAULT_MODEL_NAME = "North Dakota V2 (Default)"

def process_model_name(model_name):
  cleaned_name = re.sub(r'[🗺️👀📡]', '', model_name)
  cleaned_name = re.sub(r'[^a-zA-Z0-9()-]', '', cleaned_name)
  return cleaned_name.replace(' ', '').replace('(Default)', '').replace('-', '')

class ModelManager:
  def __init__(self):
    self.params = Params()
    self.params_memory = Params("/dev/shm/params")

  def handle_error(self, destination, error_message, error):
    print(f"Error occurred: {error}")
    self.params_memory.put("ModelDownloadProgress", error_message)
    self.params_memory.remove("DownloadAllModels")
    self.params_memory.remove("ModelToDownload")
    if destination:
      delete_file(destination)

  def download_file(self, destination, url):
    try:
      with requests.get(url, stream=True, timeout=5) as r:
        r.raise_for_status()
        total_size = get_remote_file_size(url)
        downloaded_size = 0

        with open(destination, 'wb') as f:
          for chunk in r.iter_content(chunk_size=8192):
            if self.params_memory.get_bool("CancelModelDownload"):
              self.handle_error(destination, "Download cancelled...", "Download cancelled...")
              return

            if chunk:
              f.write(chunk)
              downloaded_size += len(chunk)
              progress = (downloaded_size / total_size) * 100

              if progress != 100:
                self.params_memory.put("ModelDownloadProgress", f"{progress:.0f}%")
              else:
                self.params_memory.put("ModelDownloadProgress", "Verifying authenticity...")

    except requests.HTTPError as http_error:
      self.handle_error(destination, f"Failed: Server error ({http_error.response.status_code})", http_error)
    except requests.ConnectionError as connection_error:
      self.handle_error(destination, "Failed: Connection dropped...", connection_error)
    except requests.Timeout as timeout_error:
      self.handle_error(destination, "Failed: Download timed out...", timeout_error)
    except requests.RequestException as request_error:
      self.handle_error(destination, "Failed: Network request error. Check connection.", request_error)
    except Exception as e:
      self.handle_error(destination, "Failed: Unexpected error.", e)

  def handle_existing_model(self, model):
    print(f"Model {model} already exists, skipping download...")
    self.params_memory.put("ModelDownloadProgress", "Model already exists...")
    self.params_memory.remove("ModelToDownload")

  def handle_verification_failure(self, model, model_path):
    if self.params_memory.get_bool("CancelModelDownload"):
      return

    print(f"Verification failed for model {model}. Retrying from GitLab...")
    model_url = f"{GITLAB_URL}Models/{model}.thneed"
    self.download_file(model_path, model_url)

    if verify_download(model_path, model_url):
      print(f"Model {model} redownloaded and verified successfully from GitLab.")
    else:
      self.handle_error(model_path, "GitLab verification failed", "Verification failed")

  def download_model(self, model_to_download):
    model_path = os.path.join(MODELS_PATH, f"{model_to_download}.thneed")
    if os.path.exists(model_path):
      self.handle_existing_model(model_to_download)
      return

    self.repo_url = get_repository_url()
    if not self.repo_url:
      self.handle_error(model_path, "GitHub and GitLab are offline...", "Repository unavailable")
      return

    model_url = f"{self.repo_url}Models/{model_to_download}.thneed"
    print(f"Downloading model: {model_to_download}")
    self.download_file(model_path, model_url)

    if verify_download(model_path, model_url):
      print(f"Model {model_to_download} downloaded and verified successfully!")
      self.params_memory.put("ModelDownloadProgress", "Downloaded!")
      self.params_memory.remove("ModelToDownload")
    else:
      self.handle_verification_failure(model_to_download, model_path)

  def fetch_models(self, url):
    with urllib.request.urlopen(url) as response:
      return json.loads(response.read().decode('utf-8'))['models']

  def update_model_params(self, model_info):
    available_models, available_model_names, experimental_models, navigation_models, radarless_models = [], [], [], [], []

    for model in model_info:
      available_models.append(model['id'])
      available_model_names.append(model['name'])

      if model.get("experimental", False):
        experimental_models.append(model['id'])
      if "🗺️" in model['name']:
        navigation_models.append(model['id'])
      if "📡" not in model['name']:
        radarless_models.append(model['id'])

    self.params.put_nonblocking("AvailableModels", ','.join(available_models))
    self.params.put_nonblocking("AvailableModelsNames", ','.join(available_model_names))
    self.params.put_nonblocking("ExperimentalModels", ','.join(experimental_models))
    self.params.put_nonblocking("NavigationModels", ','.join(navigation_models))
    self.params.put_nonblocking("RadarlessModels", ','.join(radarless_models))
    print("Models list updated successfully.")

    if available_models:
      models_downloaded = self.are_all_models_downloaded(available_models, available_model_names)
      self.params.put_bool_nonblocking("ModelsDownloaded", models_downloaded)

  def are_all_models_downloaded(self, available_models, available_model_names):
    automatically_update_models = self.params.get_bool("AutomaticallyUpdateModels")
    all_models_downloaded = True

    for model in available_models:
      model_path = os.path.join(MODELS_PATH, f"{model}.thneed")
      model_url = f"{self.repo_url}Models/{model}.thneed"

      if os.path.exists(model_path):
        if automatically_update_models:
          local_file_size = os.path.getsize(model_path)
          remote_file_size = get_remote_file_size(model_url)

          if remote_file_size and remote_file_size != local_file_size:
            print(f"Model {model} is outdated. Re-downloading...")
            delete_file(model_path)
            self.remove_model_params(available_model_names, model)
            self.queue_model_download(model)
            all_models_downloaded = False
      else:
        if automatically_update_models:
          print(f"Model {model} isn't downloaded. Downloading...")
          self.remove_model_params(available_model_names, model)
          self.queue_model_download(model)
        all_models_downloaded = False

    return all_models_downloaded

  def remove_model_params(self, available_model_names, model):
    part_model_param = process_model_name(available_model_names[available_models.index(model)])
    self.params.remove(part_model_param + "CalibrationParams")
    self.params.remove(part_model_param + "LiveTorqueParameters")

  def queue_model_download(self, model, model_name=None):
    while self.params_memory.get("ModelToDownload", encoding='utf-8'):
      time.sleep(1)

    self.params_memory.put("ModelToDownload", model)
    if model_name:
      self.params_memory.put("ModelDownloadProgress", f"Downloading {model_name}...")

  def validate_models(self):
    current_model = self.params.get("Model", encoding='utf-8')
    current_model_name = self.params.get("ModelName", encoding='utf-8')

    if "(Default)" in current_model_name and current_model_name != DEFAULT_MODEL_NAME:
      self.params.put_nonblocking("ModelName", current_model_name.replace(" (Default)", ""))

    available_models = self.params.get("AvailableModels", encoding='utf-8')
    if not available_models:
      return

    for model_file in os.listdir(MODELS_PATH):
      if model_file.replace(".thneed", "") not in available_models.split(','):
        if model_file == current_model:
          self.params.put_nonblocking("Model", DEFAULT_MODEL)
          self.params.put_nonblocking("ModelName", DEFAULT_MODEL_NAME)
        delete_file(os.path.join(MODELS_PATH, model_file))
        print(f"Deleted model file: {model_file}")

  def copy_default_model(self):
    default_model_path = os.path.join(MODELS_PATH, f"{DEFAULT_MODEL}.thneed")

    if not os.path.exists(default_model_path):
      source_path = os.path.join(BASEDIR, "selfdrive", "modeld", "models", "supercombo.thneed")

      if os.path.exists(source_path):
        shutil.copyfile(source_path, default_model_path)
        print(f"Copied default model from {source_path} to {default_model_path}")
      else:
        print(f"Source default model not found at {source_path}. Exiting...")

  def update_models(self, boot_run=True):
    if boot_run:
      self.copy_default_model()
      self.validate_models()

    self.repo_url = get_repository_url()
    if not self.repo_url:
      return

    model_info = self.fetch_models(f"{self.repo_url}Versions/model_names_{VERSION}.json")
    if model_info:
      self.update_model_params(model_info)

  def download_all_models(self):
    self.repo_url = get_repository_url()
    if not self.repo_url:
      self.handle_error(None, "GitHub and GitLab are offline...", "Repository unavailable")
      return

    model_info = self.fetch_models(f"{self.repo_url}Versions/model_names_{VERSION}.json")
    if not model_info:
      self.handle_error(None, "Unable to update model list...", "Model list unavailable")
      return

    available_models = self.params.get("AvailableModels", encoding='utf-8').split(',')
    available_model_names = self.params.get("AvailableModelsNames", encoding='utf-8').split(',')

    for model in available_models:
      if self.params_memory.get_bool("CancelModelDownload"):
        return

      if not os.path.exists(os.path.join(MODELS_PATH, f"{model}.thneed")):
        model_index = available_models.index(model)
        model_name = available_model_names[model_index]

        cleaned_model_name = re.sub(r'[🗺️👀📡]', '', model_name).strip()
        print(f"Downloading model: {cleaned_model_name}")

        self.queue_model_download(model, cleaned_model_name)

        while self.params_memory.get("ModelToDownload", encoding='utf-8'):
          time.sleep(1)

    while not all(os.path.exists(os.path.join(MODELS_PATH, f"{model}.thneed")) for model in available_models):
      time.sleep(1)

    self.params_memory.put("ModelDownloadProgress", "All models downloaded!")
    self.params_memory.remove("DownloadAllModels")
    self.params.put_bool_nonblocking("ModelsDownloaded", True)
