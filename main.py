import argparse
import requests
import time
import random
import logging
import os
import json
from datetime import datetime, timedelta, timezone
import signal
from satnogsdecoders.decode_frame import decode_frame_to_dict

TELEMETRY_URL = "https://db.satnogs.org/api/telemetry/"
TELEMETRY_RATE = 240/(60*60) # Satnogs API allows 240 requests per hour, so we can make 1 request every 15 seconds
SATELLITE_URL = "https://db.satnogs.org/api/satellites/"
SATELLITE_RATE = 1

TELEMETRY_FREQUENCY = 1/TELEMETRY_RATE # Time to wait between requests
SATELLITE_FREQUENCY = 1/SATELLITE_RATE

HIGHEST_NORAD_ID = 99999 # This is an arbitrary number that is higher than the highest NORAD_ID in the database. We will loop through all possible NORAD_IDs until we reach this number.

DATA_DIR = "data"
LOGS_DIR = "logs"
CHECKPOINTS_DIR = "checkpoints"


def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_json(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w") as f:
        json.dump(state, f, indent=4)

        f.flush()
        os.fsync(f.fileno())

def save_jsonl(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "a") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")

        f.flush()
        os.fsync(f.fileno())


def build_request(cursor, start_time, end_time, norad_id):
    if cursor:
        return cursor, None # In the middle of downloading

    params = {"satellite": norad_id, "format": "json", "end": start_time}

    if end_time:
        params["start"] = end_time

    return TELEMETRY_URL, params # Starting a new download

def is_valid_NORAD_ID(norad_id):
    #! This is a very basic check, but it can help us avoid making unnecessary requests to the API/.json's.
    return isinstance(norad_id, int) and norad_id > 0 and norad_id <= HIGHEST_NORAD_ID

def load_frames(norad_id):
    frames = []
    with open(f"{DATA_DIR}/frames/{norad_id}.jsonl", "r") as f:
        lines = f.readlines()

    for line in lines:
        data = json.loads(line.strip())
        bindata = bytes.fromhex(data["frame"])
        frames.append(bindata)

    return frames




class SatnogsAPIHandler:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Token {self.api_key}"}
        self.formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.logger = logging.getLogger("SatnogsAPIHandler")

        self.stop_requested = False
        signal.signal(signal.SIGINT, self._handle_sigint)

    def _handle_sigint(self, signum, frame):
        self.stop_requested = True
        print("SIGINT received. Stopping gracefully... Please wait for the current operation to finish.")

    def make_directories(self):
        directories = [
            DATA_DIR + "/satellites", # JSON with API result for each satellite
            DATA_DIR + "/frames", # JSONL with the frames for each satellite
            DATA_DIR + "/telemetry", # csv with the decoded data for each satellite
            LOGS_DIR + "/frames",
            LOGS_DIR + "/telemetry",
            CHECKPOINTS_DIR + "/frames",
            CHECKPOINTS_DIR + "/telemetry"
        ]

        for directory in directories:
            if not os.path.exists(directory):
                os.makedirs(directory)

    def setup_logger(self, log_file):
        handler = logging.FileHandler(log_file, mode='a')
        handler.setFormatter(self.formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        return handler

    def remove_logger(self, handler):
        handler.close()
        self.logger.removeHandler(handler)

    def make_request(self, url, params=None, max_retries=8, timeout=30):

        for attempt in range(max_retries):
            try:
                resp = requests.get(url, headers=self.headers, params=params, timeout=timeout)

                if resp.status_code == 429: # Handle rate limiting
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if retry_after else (2 ** attempt)
                    self.logger.warning(f"Rate limited. Waiting {wait}s before retry.")
                    time.sleep(wait)
                    continue

                if 500 <= resp.status_code < 600: # Retry on server errors
                    if attempt == max_retries - 1:
                        self.logger.error(f"Server error {resp.status_code}. No more retries left.")
                        raise requests.exceptions.HTTPError(f"Server error {resp.status_code}")
                    else:
                        wait = 2 ** attempt
                        self.logger.warning(f"Server error {resp.status_code}. Retrying in {wait}s.")
                        time.sleep(wait)
                        continue

                # Raise for other bad responses
                resp.raise_for_status()

                return resp.json()

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as e:

                if attempt == max_retries - 1:
                    self.logger.error(f"Request failed after {max_retries} attempts: {e}")
                    raise

                # Exponential backoff with jitter
                backoff = (2 ** attempt) + random.uniform(0, 1)
                self.logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. "f"Retrying in {backoff:.2f}s")
                time.sleep(backoff)


    def test_api_connection(self):
        
        handler = self.setup_logger(f'{LOGS_DIR}/api_test.log')

        flag = True
        # Test with BugSat-1, which has NORAD_ID 40014
        response = self.make_request(SATELLITE_URL, params={"norad_cat_id": 40014, "format": "json"})

        if len(response) != 1:
            self.logger.error(f"Expected 1 satellite, but got {len(response)}")
            flag = False
        
        satellite_info = response[0]
        expected_fields = ["sat_id", "norad_cat_id", "name", "status", "decayed", "launched", "telemetries"]
        for field in expected_fields:
            if field not in satellite_info:
                self.logger.error(f"Field {field} is missing in the response")
                flag = False

        # Test if we can obtain the lastest telemetry for BugSat-1
        response = self.make_request(TELEMETRY_URL, params={"satellite": 40014, "format": "json"})

        # Expected to receive a list dictionary with 3 fields: next, previous and results
        expected_fields = ["next", "previous", "results"]
        for field in expected_fields:
            if field not in response:
                self.logger.error(f"Field {field} is missing in the telemetry response")
                flag = False

        data = response.get("results", [])

        # Results should have a list of 25 points
        if data is None:
            self.logger.error("Results field is missing in the telemetry response")
            flag = False

        if isinstance(data, list):
            if len(data) != 25:
                self.logger.error(f"Expected 25 telemetry points, but got {len(data)}")
                flag = False

            # Each telemetry point should have the following fields: sat_id, norad_cat_id, timestamp, frame, observer
            expected_fields = ["sat_id", "norad_cat_id", "timestamp", "frame", "observer"]
            for point in data:
                for field in expected_fields:
                    if field not in point:
                        self.logger.error(f"Field {field} is missing in telemetry point: {point}")
                        flag = False
    
        if flag:
            self.logger.info("API connection test completed successfully.")
        else:
            self.logger.error("API connection test failed. Please check the issues above.")

        self.remove_logger(handler)


    def get_satellite(self, norad_id: int):
        data = self.make_request(SATELLITE_URL, params={"norad_cat_id": norad_id, "format": "json"})

        if data: # If the response is not empty, it means that the satellite exists
            self.logger.info(f"Found satellite with NORAD_ID {norad_id}")
            save_json(f"{DATA_DIR}/satellites/{norad_id}.json", data[0])
        else:
            self.logger.info(f"No satellite found with NORAD_ID {norad_id}")

    def get_all_satellites(self):
        #! The API doesn't have a way to get all available NORAD_IDs, so we have to loop through all possible IDs and check if they exist.
        # If the program is interrupted, we can check the logs to see where it left off and resume from there.

        state = load_json(f"{CHECKPOINTS_DIR}/satellites.json")
        finished = state.get("finished", False) 
        last_norad_id = state.get("last_norad_id", 0)
        if finished: # Are we in the middle of a download?
            # Means that we downloaded all satellite, redo the download again.
            print("All satellite data has already been downloaded previously. Updating all information.")
        else:
            print(f"Resuming from NORAD_ID {last_norad_id + 1}")

        handler = self.setup_logger(f'{LOGS_DIR}/satellites.log')

        for norad_id in range(last_norad_id + 1, HIGHEST_NORAD_ID + 1):
            self.get_satellite(norad_id)
            save_json(f"{CHECKPOINTS_DIR}/satellites.json", {"finished": False, "last_norad_id": norad_id}) 
            time.sleep(SATELLITE_FREQUENCY) # Be kind to the API
            if self.stop_requested: # For Ctl + C handling
                return

        save_json(f"{CHECKPOINTS_DIR}/satellites.json", {"finished": True, "last_norad_id": HIGHEST_NORAD_ID})
        self.remove_logger(handler)

    def get_norad_ids_from_files(self):
        norad_ids = []
        for filename in os.listdir(f"{DATA_DIR}/satellites"): # Get all NORAD_IDs of the satellites in the data/satellites directory
            if filename.endswith(".json"):
                norad_id = int(filename.split(".json")[0])
                norad_ids.append(norad_id)
        return norad_ids
    
    def get_decodable_norad_ids(self):
        norad_ids = self.get_norad_ids_from_files()

        for norad_id in norad_ids:
            state = load_json(f"{DATA_DIR}/satellites/{norad_id}.json")
            if len(state.get("telemetries", [])) > 0: # Has at least one decoder
                yield norad_id


    def get_frames(self, norad_id: int):
        #! Satnogs works by providing the newest telemetry data first. No sorting options are available.
        #! To make sure that we know until when we have downloaded, we will save the cursor and timestamp of when we started downloading.
        #! Why? Because, the API works via pagination, that means, even if we take 1 month downloading the data, we know that we can download all data since "start_time".
        #! After finishing the download, we will update the "start_time" to be the current time and download until "start_time".

        state = load_json(f"{CHECKPOINTS_DIR}/frames/{norad_id}.json")
        start_time = state.get("start_time", datetime.now(timezone.utc).isoformat())
        end_time = state.get("end_time", None) # Could be None if we haven't finished a complete download before else a timestamp
        cursor = state.get("cursor", None)
        finished = state.get("finished", False)

        if finished:
            print("Previous download completed. Updating the previous download.")
            #! If the frame timestamp is equal to end_time, it will repeat
            # Add 1 microsecond to the end_time to avoid repeating the last downloaded frame
            end_time = (datetime.fromisoformat(start_time) + timedelta(microseconds=1)).isoformat()
            start_time = datetime.now(timezone.utc).isoformat()
            cursor = None      

        state = {"start_time": start_time, "end_time": end_time, "cursor": cursor, "finished": False}

        handler = self.setup_logger(f'{LOGS_DIR}/frames/{norad_id}.log')

        # Download Loop
        while True:
            
            url, params = build_request(cursor, start_time, end_time, norad_id)

            data = self.make_request(url, params=params)
            # It is here because if the first download is empty, it will break the loop and we don't want to make too many requests in a short period of time.
            time.sleep(TELEMETRY_FREQUENCY)

            results = data.get("results", [])
            next_cursor = data.get("next")

            if not results: # No more results to download, we are done.
                self.logger.info("No more results returned.")
                break

            save_jsonl(f"{DATA_DIR}/frames/{norad_id}.jsonl", results)

            self.logger.info(f"Downloaded {len(results)} frames. Next cursor: {next_cursor}")

            # Update Checkpoint
            state["cursor"] = next_cursor
            state["finished"] = False
            
            save_json(f"{CHECKPOINTS_DIR}/frames/{norad_id}.json", state)

            if not next_cursor: # No more pages to download
                break

            if self.stop_requested: # For Ctl + C handling
                return

            cursor = next_cursor

        # Mark as finished
        state["finished"] = True
        state["cursor"] = None

        save_json(f"{CHECKPOINTS_DIR}/frames/{norad_id}.json", state)
        self.logger.info(f"Finished downloading telemetry data up to timestamp {start_time}")
        self.remove_logger(handler)
        

    def decode_frames(self, norad_id: int):
        # Load decoder name
        satellite_info = load_json(f"{DATA_DIR}/satellites/{norad_id}.json")
        decoders = satellite_info.get("telemetries", [])
        if len(decoders) > 0:
            decoder_name = decoders[0].get("decoder", "Unknown Decoder")
            self.logger.info(f"Found decoder {decoder_name} for satellite {norad_id}")
            if decoder_name == "Unknown Decoder":
                self.logger.error(f"Decoder name is unknown for satellite {norad_id}.")
        else:
            self.logger.error(f"No decoders found for satellite {norad_id}")
            return

        # Load the frames
        frames = load_frames(norad_id)

        # Checkpoint
        state = load_json(f"{CHECKPOINTS_DIR}/telemetry/{norad_id}.json")
        last_decoded_index = state.get("last_decoded_index", -1)

        # Logger
        handler = self.setup_logger(f'{LOGS_DIR}/telemetry/{norad_id}.log')

        for i in range(last_decoded_index + 1, len(frames)):
            frame = frames[i]
            try:
                decoded = decode_frame_to_dict(decoder_name, frame)
                save_jsonl(f"{DATA_DIR}/telemetry/{norad_id}.jsonl", [decoded])
                self.logger.info(f"Decoded frame {i+1}/{len(frames)}")
            except Exception as e:
                self.logger.error(f"Error decoding frame {i+1}: {e}")

            state["last_decoded_index"] = i
            save_json(f"{CHECKPOINTS_DIR}/telemetry/{norad_id}.json", state)

            if self.stop_requested: # For Ctl + C handling
                return

        self.logger.info(f"Finished decoding telemetry data for satellite {norad_id}")
        self.remove_logger(handler)






if __name__ == "__main__":
    
    # API Key Input
    parser = argparse.ArgumentParser(description="Download & Decode satellite telemetry data from Satnogs API.")
    parser.add_argument("--api_key", type=str, required=True, help="Satnogs API key. You can get it from https://db.satnogs.org/profile/ after creating an account.")
    subparsers = parser.add_subparsers(dest="mode", required=True, help="Operation mode")

    subparsers.add_parser("test-api", help="Test API connectivity")
    subparsers.add_parser("run-all", help="Run full pipeline")
    subparsers.add_parser("download-all-satellites", help="Download all satellites")
    download_sat = subparsers.add_parser("download-satellite", help="Download specific satellite")
    download_sat.add_argument("--norad", required=True, type=int, help="NORAD ID of satellite")
    subparsers.add_parser("download-all-frames", help="Download all frames")
    download_frames = subparsers.add_parser("download-frames",help="Download frames for specific satellite")
    download_frames.add_argument("--norad", required=True, type=int, help="NORAD ID of satellite")
    subparsers.add_parser("decode-all-frames", help="Decode all frames")
    decode_frame = subparsers.add_parser("decode-frame", help="Decode frames for specific satellite")
    decode_frame.add_argument("--norad", required=True, type=int, help="NORAD ID of satellite")

    args = parser.parse_args()

    handler = SatnogsAPIHandler(args.api_key)
    handler.make_directories()

    # ---- Mode Handling ----
    if args.mode == "test-api":
        print("Testing API with key:", args.api_key)
        # Verify if the code connects to the API correctly (basically, check if the code is outdated)
        handler.test_api_connection()

    elif args.mode == "run-all":
        pass

    elif args.mode == "download-all-satellites":
        print("Downloading all satellites...")
        handler.get_all_satellites()

    elif args.mode == "download-satellite":
        if not is_valid_NORAD_ID(args.norad):
            print(f"Invalid NORAD ID: {args.norad}")
            exit(1)
        print(f"Downloading satellite with NORAD ID {args.norad}...")
        h = handler.setup_logger(f'{handler.LOGS_DIR}/satellites.log')
        handler.get_satellite(args.norad)
        handler.remove_logger(h)

    elif args.mode == "download-all-frames":
        print("Downloading frames for all satellites...")
        norad_ids = list(handler.get_decodable_norad_ids())
        for i, norad_id in enumerate(norad_ids):
            print(f"Processing satellite {i+1}/{len(norad_ids)}: NORAD ID {norad_id}")
            handler.get_frames(norad_id)

    elif args.mode == "download-frames":
        if not is_valid_NORAD_ID(args.norad):
            print(f"Invalid NORAD ID: {args.norad}")
            exit(1)
        print(f"Downloading frames for satellite with NORAD ID {args.norad}...")
        handler.get_frames(args.norad)

    elif args.mode == "decode-all-frames":
        print("Decoding frames for all satellites...")
        norad_ids = list(handler.get_decodable_norad_ids())
        for i, norad_id in enumerate(norad_ids):
            print(f"Processing satellite {i+1}/{len(norad_ids)}: NORAD ID {norad_id}")
            handler.decode_frames(norad_id)


    elif args.mode == "decode-frame":
        if not is_valid_NORAD_ID(args.norad):
            print(f"Invalid NORAD ID: {args.norad}")
            exit(1)
        print(f"Decoding frames for satellite with NORAD ID {args.norad}...")
        handler.decode_frames(args.norad)
    