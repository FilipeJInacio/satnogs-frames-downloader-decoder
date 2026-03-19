# SatNOGS Telemetry Downloader & Decoder

This project provides a complete pipeline to:

1. Download satellite metadata from the SatNOGS database  
2. Download raw telemetry frames from the SatNOGS API  
3. Decode telemetry frames using the `satnogsdecoders` library  
4. Store raw and decoded data locally with checkpointing and logging  

The code is designed for long-running downloads, robustness against interruptions and safe interaction with API rate limits.

---

## Requirements

1. Install the latest SatNOGS Decoders.
Visit SatNOGS Decoders on GitLab (https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders) and follow the instructions provided to compile the decoders. Once the process is complete, copy the `satnogsdecoders` folder and place it in the same directory as `main.py`.

3. Install the required Python packages. Python 3.8 or higher is recommended.
```
pip install -r requirements.txt
```

---

## Usage

```
python main.py --api_key YOUR_API_KEY <mode> [options]
```

1. Verifies that the API key is valid and that the endpoints respond correctly.
```
python main.py --api_key YOUR_API_KEY test-api
```

2. Downloads satellite metadata, retrieves telemetry frames for all decodable satellites and decodes them.
```
python main.py --api_key YOUR_API_KEY run-all 
```

3. Downloads metadata for all possible NORAD IDs.
```
python main.py --api_key YOUR_API_KEY download-all-satellites
```

4. Download metadata for a specific satellite
```
python main.py --api_key YOUR_API_KEY download-satellite --norad NORAD
```

5. Download frames for all decodable satellites
```
python main.py --api_key YOUR_API_KEY download-frames-all-satellites
```

6. Download frames for one satellite
```
python main.py --api_key YOUR_API_KEY download-frames --norad NORAD
```

7. Decode frames for all satellites
```
python main.py --api_key YOUR_API_KEY decode-frames-all-satellites
```

8. Decode frames for one satellite
```
python main.py --api_key YOUR_API_KEY decode-frames --norad NORAD
```

Example:
Decode all frames from Bugsat-1 (40014)
```
python main.py --api_key 0000000000000000000000000000000000000000 decode-frames --norad 40014
```

---

## API Key

You must create an account at:

https://db.satnogs.org/

Then obtain an API key from your profile page.
