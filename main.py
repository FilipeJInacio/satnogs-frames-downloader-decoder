import argparse
import requests
from time import sleep
import logging
import os
import json
from datetime import datetime, timedelta, timezone

# TODO:
# FIle Paths
# File flush

TELEMETRY_URL = "https://db.satnogs.org/api/telemetry/"
SATELLITE_URL = "https://db.satnogs.org/api/satellites/"
HIGHEST_NORAD_ID = 99999 # This is an arbitrary number that is higher than the highest NORAD_ID in the database. We will loop through all possible NORAD_IDs until we reach this number.
RATE = 240/(60*60) # Satnogs API allows 240 requests per hour
FREQUENCY = 1/RATE # Time to wait between requests

class SatnogsAPIHandler:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Token {self.api_key}"}
        self.formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    def test_api_connection(self):
        flag = True
        # Test with BugSat-1, which has NORAD_ID 40014
        response = self.get_satellite(40014)

        if len(response) != 1:
            print(f"Expected 1 satellite, but got {len(response)}")
            flag = False
        
        satellite_info = response[0]
        expected_fields = ["sat_id", "norad_cat_id", "name", "status", "decayed", "launched", "telemetries"]
        for field in expected_fields:
            if field not in satellite_info:
                print(f"Field {field} is missing in the response")
                flag = False

        # Test if we can obtain the lastest telemetry for BugSat-1
        response = self.get_telemetry(40014)

        # Expected to receive a list dictionary with 3 fields: next, previous and results
        expected_fields = ["next", "previous", "results"]
        for field in expected_fields:
            if field not in response:
                print(f"Field {field} is missing in the telemetry response")
                flag = False

        data = response.get("results", [])

        # Results should have a list of 25 points
        if data is None:
            print("Results field is missing in the telemetry response")
            flag = False

        if isinstance(data, list):
            if len(data) != 25:
                print(f"Expected 25 telemetry points, but got {len(data)}")
                flag = False

            # Each telemetry point should have the following fields: sat_id, norad_cat_id, timestamp, frame, observer
            expected_fields = ["sat_id", "norad_cat_id", "timestamp", "frame", "observer"]
            for point in data:
                for field in expected_fields:
                    if field not in point:
                        print(f"Field {field} is missing in telemetry point: {point}")
                        flag = False
    
        if flag:
            print("API connection test completed successfully.")
        else:
            print("API connection test failed. Please check the issues above.")

    def get_satellite(self, norad_id: int):
        params = {"norad_cat_id": norad_id, "format": "json"}
        resp = requests.get(SATELLITE_URL, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def get_telemetry(self, norad_id: int):
        params = {"satellite": norad_id, "format": "json"}
        resp = requests.get(TELEMETRY_URL, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_all_satellites(self):
        # Gets the JSON information of all satellites in the Satnogs database and saves it in data/satellites/{norad_id}.json. The NORAD_IDs of the satellites are obtained from the logs/satellites.log file, which is created during the execution of this function. If the program is interrupted, we can check the logs to see where it left off and resume from there.

        # See the last log message from logs/satellites.log to get the last NORAD_ID that was processed
        last_norad_id = 0
        if os.path.exists("logs/satellites.log"):
            with open("logs/satellites.log", "r") as f:
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
            return

        logger = logging.getLogger()
        # if it doesn't exist, create a log file in logs/satellites.log
        if not os.path.exists("logs/satellites.log"):
            handler = logging.FileHandler('logs/satellites.log', mode='w')
        else:
            handler = logging.FileHandler('logs/satellites.log', mode='a')

        handler.setFormatter(self.formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # because Satnogs API doesn't have a way to get all available NORAD_IDs, we have to loop through all possible IDs
        #! This loop believes that no new NORAD_ID will be added in a range that was once fully processed. 
        #! For example, if we process all NORAD_IDs from 1 to 100000, we assume that no new NORAD_ID will be added in this range in the future. 
        for norad_id in range(last_norad_id + 1, HIGHEST_NORAD_ID + 1):
            print(f"Processing NORAD_ID {norad_id}...")
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
            
            sleep(1) # Be kind to the API # 1 seconds because many will miss

        handler.close()
        logger.removeHandler(handler)

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
    parser = argparse.ArgumentParser(description="Download satellite telemetry data from Satnogs API.")
    parser.add_argument("--api_key", type=str, required=True, help="Satnogs API key. You can get it from https://db.satnogs.org/profile/ after creating an account.")
    args = parser.parse_args()

    if not args.api_key:
        print("API key is required. Please provide it using the --api_key argument.")
        exit(1)
    
    handler = SatnogsAPIHandler(args.api_key)

    # Verify if the functions work correctly (basically, check if the code is outdated)
    # handler.test_api_connection()

    # Create a data folder if it doesn't exist
    if not os.path.exists("data"):
        os.makedirs("data") # This is where all data will be stored

    if not os.path.exists("data/satellites"):
        os.makedirs("data/satellites") # This is where the information of each satellite will be stored in a json file named {norad_id}.json

    if not os.path.exists("data/telemetry"):
        os.makedirs("data/telemetry") # This is where the telemetry data for each satellite will be stored in json files named {norad_id}.jsonl


    # Create a logs folder if it doesn't exist
    if not os.path.exists("logs"):
        os.makedirs("logs") # This is where the progress of download will be stored. If the program is interrupted, we can check the logs to see where it left off and resume from there.

    if not os.path.exists("logs/telemetry"):
        os.makedirs("logs/telemetry") # This is where the logs for telemetry download will be stored in files named {norad_id}.log


    if not os.path.exists("checkpoints"):
        os.makedirs("checkpoints")

    if not os.path.exists("checkpoints/telemetry"):
        os.makedirs("checkpoints/telemetry")

    handler.get_all_satellites()

    # Get all NORAD_IDs of the satellites in Satnogs that we found from logs/satellites.log
    #norad_ids = handler.get_norad_ids_from_log()

    # Get all NORAD_IDs of the satellites that have a functioning decoder
    norad_ids = list(handler.get_norad_ids_with_decoders_from_log())

    # For each NORAD_ID, get all telemetry data (if all data is already downloaded, just verify if there is any new telemetry data and download it)
    for i, norad_id in enumerate(norad_ids):
        print(f"Processing satellite {i+1}/{len(norad_ids)}: NORAD ID {norad_id}")
        handler.get_all_telemetry(norad_id)