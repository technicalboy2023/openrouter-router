from fastapi import FastAPI
import requests
import json
import time
import os
from dotenv import load_dotenv

app = FastAPI()

# -------------------------
# LOAD ENV
# -------------------------

load_dotenv()

HF_KEYS = [
os.getenv("HF_KEY_1"),
os.getenv("HF_KEY_2"),
os.getenv("HF_KEY_3"),
os.getenv("HF_KEY_4"),
os.getenv("HF_KEY_5"),
os.getenv("HF_KEY_6"),
os.getenv("HF_KEY_7"),
os.getenv("HF_KEY_8"),
os.getenv("HF_KEY_9"),
os.getenv("HF_KEY_10")
]

# remove empty keys
HF_KEYS = [k for k in HF_KEYS if k]

DEFAULT_MODEL = "google/gemma-3-4b-it"

session = requests.Session()

key_index = 0


# -------------------------
# KEY ROTATION
# -------------------------

def get_key():

    global key_index

    key = HF_KEYS[key_index]

    key_index = (key_index + 1) % len(HF_KEYS)

    return key


# -------------------------
# HF CHAT CALL
# -------------------------

def hf_chat(model,messages):

    key = get_key()

    url = "https://router.huggingface.co/v1/chat/completions"

    headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
    }

    payload = {
    "model": model,
    "messages": messages,
    "max_tokens": 512
    }

    try:

        r = session.post(url,json=payload,headers=headers,timeout=120)

        if r.status_code == 200:

            data = r.json()

            return data["choices"][0]["message"]["content"]

        else:

            return f"HF error {r.status_code}"

    except Exception as e:

        return str(e)


# -------------------------
# CHAT ENDPOINT
# -------------------------

@app.post("/v1/chat/completions")
async def chat(data:dict):

    messages=data.get("messages",[])
    model=data.get("model",DEFAULT_MODEL)

    reply = hf_chat(model,messages)

    return{
    "id":"chatcmpl-"+str(int(time.time())),
    "object":"chat.completion",
    "created":int(time.time()),
    "model":model,
    "choices":[
        {
        "index":0,
        "message":{
            "role":"assistant",
            "content":reply
        },
        "finish_reason":"stop"
        }
    ]
    }


# -------------------------
# MODELS
# -------------------------

@app.get("/v1/models")
async def models():

    r = session.get("https://huggingface.co/api/models?pipeline_tag=text-generation&limit=200")

    data=r.json()

    models=[]

    for m in data:

        models.append({
        "id":m["modelId"],
        "object":"model",
        "owned_by":"huggingface"
        })

    return{
    "object":"list",
    "data":models
    }


# -------------------------
# HEALTH
# -------------------------

@app.get("/health")
async def health():

    return{
    "status":"ok",
    "keys":len(HF_KEYS)
    }
