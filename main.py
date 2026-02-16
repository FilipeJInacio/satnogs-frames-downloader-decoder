import argparse
import requests
import time
import random
import logging
import os
import json
from datetime import datetime, timedelta, timezone

# TODO:
# FIle Paths
# File flush

TELEMETRY_URL = "https://db.satnogs.org/api/telemetry/"
TELEMETRY_RATE = 240/(60*60) # Satnogs API allows 240 requests per hour, so we can make 1 request every 15 seconds
SATELLITE_URL = "https://db.satnogs.org/api/satellites/"
SATELLITE_RATE = 1

TELEMETRY_FREQUENCY = 1/TELEMETRY_RATE # Time to wait between requests
SATELLITE_FREQUENCY = 1/SATELLITE_RATE

HIGHEST_NORAD_ID = 99999 # This is an arbitrary number that is higher than the highest NORAD_ID in the database. We will loop through all possible NORAD_IDs until we reach this number.

class SatnogsAPIHandler:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Token {self.api_key}"}
        self.formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.logger = logging.getLogger("SatnogsAPIHandler")


    def make_directories(self):

        self.DATA_DIR = "data"
        self.LOGS_DIR = "logs"
        self.CHECKPOINTS_DIR = "checkpoints"

        directories = [
            self.DATA_DIR + "/satellites", # JSON with API result for each satellite
            self.DATA_DIR + "/frames", # JSONL with the frames for each satellite
            self.DATA_DIR + "/telemetry", # csv with the decoded data for each satellite
            self.LOGS_DIR + "/frames",
            self.LOGS_DIR + "/telemetry",
            self.CHECKPOINTS_DIR + "/frames",
            self.CHECKPOINTS_DIR + "/telemetry"
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
        
        handler = self.setup_logger('logs/api_test.log')

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
            with open(f"{self.DATA_DIR}/satellites/{norad_id}.json", "w") as f:
                json.dump(data[0], f)
        else:
            self.logger.info(f"No satellite found with NORAD_ID {norad_id}")

    def get_all_satellites(self):
        #! The API doesn't have a way to get all available NORAD_IDs, so we have to loop through all possible IDs and check if they exist.
        # If the program is interrupted, we can check the logs to see where it left off and resume from there.

        # Checkpoint logic
        last_norad_id = 0
        if os.path.exists(f"{self.CHECKPOINTS_DIR}/satellites.json"):
            with open(f"{self.CHECKPOINTS_DIR}/satellites.json", "r") as f:
                state = json.load(f)

            finished = state.get("finished", False) # Are we in the middle of a download?
            if finished:
                # Means that we downloaded all satellite, so, re:do the download again.
                last_norad_id = 0
                with open(f"{self.CHECKPOINTS_DIR}/satellites.json", "w") as f:
                    json.dump({"finished": False, "last_norad_id": 0}, f)
                print("All satellite data has already been downloaded previously. Updating all information.")
                time.sleep(10) # Sleep for 10 seconds to give the user time to read the message and cancel if they want to.
            else:
                last_norad_id = state.get("last_norad_id", 0)
                print(f"Resuming from NORAD_ID {last_norad_id + 1}")
        else:
            with open(f"{self.CHECKPOINTS_DIR}/satellites.json", "w") as f:
                json.dump({"finished": False, "last_norad_id": 0}, f)

        handler = self.setup_logger('logs/satellites.log')

        for norad_id in range(last_norad_id + 1, HIGHEST_NORAD_ID + 1):
            self.get_satellite(norad_id)
            
            with open(f"{self.CHECKPOINTS_DIR}/satellites.json", "w") as f: # Update checkpoint
                json.dump({"finished": False, "last_norad_id": norad_id}, f)

            time.sleep(SATELLITE_FREQUENCY) # Be kind to the API

        with open(f"{self.CHECKPOINTS_DIR}/satellites.json", "w") as f:
            json.dump({"finished": True, "last_norad_id": HIGHEST_NORAD_ID}, f)

        self.remove_logger(handler)

    def get_all_satellites_info(self):
        # Function that assumes that logs/satellites.log has all the NORAD_IDs of the satellites that we want to get info for.
        
        logger = logging.getLogger()
        # if it doesn't exist, create a log file in logs/satellites_info.log
        if not os.path.exists("logs/satellites_info.log"):
            handler = logging.FileHandler('logs/satellites_info.log', mode='w')
        else:
            handler = logging.FileHandler('logs/satellites_info.log', mode='a')

        handler.setFormatter(self.formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # get the last NORAD_ID that was processed from logs/satellites_info.log
        last_norad_id = 0
        if os.path.exists("logs/satellites_info.log"):
            with open("logs/satellites_info.log", "r") as f:
                lines = f.readlines()
                if lines:
                    last_line = lines[-1]
                    if "NORAD_ID" in last_line:
                        last_norad_id = int(last_line.split("NORAD_ID")[1].strip())
                        print(f"Resuming from NORAD_ID {last_norad_id + 1}")
                    else:
                        print("No NORAD_ID found in the last log message. Starting from the beginning.")
                else:
                    print("Log file is empty. Starting from the beginning.")
        
        if last_norad_id >= HIGHEST_NORAD_ID:
            print(f"All NORAD_IDs up to {HIGHEST_NORAD_ID} have been processed. No more satellites to check.")
            handler.close()
            logger.removeHandler(handler)
            return

        if os.path.exists("logs/satellites.log"):
            with open("logs/satellites.log", "r") as f:
                lines = f.readlines()
                for line in lines:
                    if "Found satellite with NORAD_ID" in line:
                        norad_id = int(line.split("Found satellite with NORAD_ID")[1].strip())
                        try:
                            response = self.get_satellite(norad_id)
                            if response: # If the response is not empty, it means that the satellite exists
                                logging.info(f"Found satellite with NORAD_ID {norad_id}")
                                # Save the response in a json file in data/satellites/{norad_id}.json
                                with open(f"data/satellites/{norad_id}.json", "w") as f:
                                    json.dump(response[0], f)
                            else:
                                logging.info(f"No satellite found with NORAD_ID {norad_id}")
                        except requests.HTTPError as e:
                            if e.response.status_code == 404:
                                logging.info(f"No satellite found with NORAD_ID {norad_id}")
                            else:
                                logging.error(f"HTTP error occurred: {e} for NORAD_ID {norad_id}")
                        except Exception as e:
                            logging.error(f"An error occurred: {e} for NORAD_ID {norad_id}")
                        
                        sleep(FREQUENCY) # Be kind to the API

        else:
            print("No log file found. Please run get_all_satellites() first to create the log file with the NORAD_IDs of the satellites.")

        handler.close()
        logger.removeHandler(handler)

    def get_norad_ids_from_log(self):
        norad_ids = []
        if os.path.exists("logs/satellites.log"):
            with open("logs/satellites.log", "r") as f:
                lines = f.readlines()
                for line in lines:
                    if "Found satellite with NORAD_ID" in line:
                        norad_id = int(line.split("Found satellite with NORAD_ID")[1].strip())
                        norad_ids.append(norad_id)
        else:
            print("No log file found. Please run get_all_satellites() first to create the log file with the NORAD_IDs of the satellites.")
        
        return norad_ids
    
    def get_norad_ids_with_decoders_from_log(self):
        norad_ids = []
        if os.path.exists("logs/satellites.log"):
            with open("logs/satellites.log", "r") as f:
                lines = f.readlines()
                for line in lines:
                    if "Found satellite with NORAD_ID" in line:
                        norad_id = int(line.split("Found satellite with NORAD_ID")[1].strip())
                        norad_ids.append(norad_id)
        else:
            print("No log file found. Please run get_all_satellites() first to create the log file with the NORAD_IDs of the satellites.")

        for norad_id in norad_ids:
            with open(f"data/satellites/{norad_id}.json", "r") as f:
                satellite_info = json.load(f)
                if len(satellite_info.get("telemetries", [])) > 0:
                    yield norad_id

    def get_all_telemetry(self, norad_id: int):
        logger = logging.getLogger()
        # if it doesn't exist, create a log file in logs/telemetry/{norad_id}.log
        if not os.path.exists(f"logs/telemetry/{norad_id}.log"):
            handler = logging.FileHandler(f'logs/telemetry/{norad_id}.log', mode='w')
        else:
            handler = logging.FileHandler(f'logs/telemetry/{norad_id}.log', mode='a')

        handler.setFormatter(self.formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # Was there progress in the download?
        if os.path.exists(f"checkpoints/telemetry/{norad_id}.json"):
            with open(f"checkpoints/telemetry/{norad_id}.json", "r") as f:
                state = json.load(f)

            start_time = state["start_time"]
            cursor = state["cursor"]
            finished = state["finished"]
            download_until = state.get("download_until")

            if finished:
                logger.info("Previous download completed. Starting incremental update.")

                download_until = start_time
                start_time = datetime.now(timezone.utc).isoformat()
                cursor = None
                state = {"start_time": start_time, "cursor": None, "finished": False, "download_until": download_until}

            #! If the frame timestamp is equal to download_until, it will repeat
            if download_until:
                # Add 1 microsecond to the download_until to avoid repeating the last downloaded frame
                download_until = (datetime.fromisoformat(download_until) + timedelta(microseconds=1)).isoformat()

        else:
            start_time = datetime.now(timezone.utc).isoformat()
            state = {"start_time": start_time, "cursor": None, "finished": False, "download_until": None}
            cursor = None
            download_until = None

        logger.info(f"Download started at {start_time}")

        # Download Loop
        while True:
            # Was there a search in progress?
            if cursor:
                url = cursor
                params = None
            else:
                url = TELEMETRY_URL
                # Was there a previous download?
                if download_until:
                    # Add the smallest possible time delta to avoid repeating the last downloaded frame

                    params = {"satellite": norad_id, "format": "json", "end": start_time, "start": download_until} 
                else:
                    params = {"satellite": norad_id, "format": "json", "end": start_time}


            # for attempt in range(5):
            #     try:
            #         resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            #         resp.raise_for_status()
            #         break
            #     except requests.RequestException as e:
            #         wait = 2 ** attempt
            #         logger.warning(f"Request failed (attempt {attempt+1}), retrying in {wait}s")
            #         time.sleep(wait)
            # else:
            #     logger.error("Max retries exceeded. Exiting.")
            #     return

            resp = requests.get(url, headers=self.headers, params=params)
            resp.raise_for_status() # and a catch for HTTP error,  502 Server Error: Bad Gateway for url
            data = resp.json()
            sleep(FREQUENCY)

            results = data.get("results", [])
            next_cursor = data.get("next")

            if not results:
                logger.info("No more results returned.")
                break

            with open(f"data/telemetry/{norad_id}.jsonl", "a") as f:
                for frame in results:
                    f.write(json.dumps(frame) + "\n")
                f.flush()
                os.fsync(f.fileno())

            logger.info(f"Downloaded {len(results)} frames. Next cursor: {next_cursor}")

            # Update Checkpoint
            state["cursor"] = next_cursor
            state["finished"] = False
            
            with open(f"checkpoints/telemetry/{norad_id}.json", "w") as f:
                json.dump(state, f)

            if not next_cursor:
                break

            cursor = next_cursor

        # Mark as finished
        state["finished"] = True
        state["cursor"] = None

        with open(f"checkpoints/telemetry/{norad_id}.json", "w") as f:
            json.dump(state, f)

        logger.info(f"Finished downloading telemetry data up to timestamp {start_time}")
        handler.close()
        logger.removeHandler(handler)



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
        handler.get_all_satellites()

    elif args.mode == "download-satellite":
        h = handler.setup_logger('logs/satellites.log')
        handler.get_satellite(args.norad)
        handler.remove_logger(h)
        
    elif args.mode == "download-all-frames":
        pass

    elif args.mode == "download-frames":
        pass

    elif args.mode == "decode-all-frames":
        pass

    elif args.mode == "decode-frame":
        pass
    




    

    # Get all NORAD_IDs of the satellites in Satnogs that we found from logs/satellites.log
    #norad_ids = handler.get_norad_ids_from_log()

    # Get all NORAD_IDs of the satellites that have a functioning decoder
    # norad_ids = list(handler.get_norad_ids_with_decoders_from_log())

    # For each NORAD_ID, get all telemetry data (if all data is already downloaded, just verify if there is any new telemetry data and download it)
    # for i, norad_id in enumerate(norad_ids):
    #     print(f"Processing satellite {i+1}/{len(norad_ids)}: NORAD ID {norad_id}")
    #     handler.get_all_telemetry(norad_id)