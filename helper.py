import json
import os
import sys
from datetime import datetime

today = datetime.now().strftime("%d-%m-%Y")
log_file = f"logs/log-{today}.log"
os.makedirs(os.path.dirname(log_file), exist_ok=True)

DEFAULT_CONFIG = {
    "host": "localhost",
    "database": "easytrade_db",
    "user": "easytrade",
    "password": "masterkey",
    "port": 3306,
    "extension_id": 10001,
    "check_time": 10,
    "lang": "ru",
}


def get_date():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def write_log_file(text):
    with open(log_file, "a", encoding="utf-8") as file:
        formatted_text = f"{get_date()} - {text}\n"
        file.write(formatted_text)
        try:
            print(formatted_text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(
                formatted_text.encode(encoding, errors="replace").decode(
                    encoding, errors="replace"
                )
            )


def configure_settings(data_dict=None, filename="config.json"):
    if data_dict is None:
        data_dict = dict(DEFAULT_CONFIG)
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except FileNotFoundError:
            write_log_file(f"Error: File '{filename}' not found")
        except json.JSONDecodeError:
            write_log_file(f"Error: File '{filename}' contains invalid JSON")
            os.remove(filename)
        except Exception as e:
            write_log_file(f"Error reading JSON file: {e}")
            os.remove(filename)

    try:
        with open(filename, "w", encoding="utf-8", errors="replace") as json_file:
            json.dump(data_dict, json_file, indent=4, ensure_ascii=False)
    except Exception as e:
        write_log_file(f"Error writing to JSON file: {e}")
    else:
        return data_dict
    return data_dict


def get_translation(lang: str = "en"):
    """Load translations from lang/{lang}.json. Fallback to en if missing."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "lang", f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        en_path = os.path.join(base, "lang", "en.json")
        try:
            with open(en_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


config = configure_settings()
translations = get_translation(config.get("lang", "en") if config else "en")
