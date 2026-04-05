import requests
import json

url = "https://api.elections.kalshi.com/trade-api/v2/category/crypto"

response = requests.get(url)
response.raise_for_status()  # fail fast if API errors

data = response.json()       # convert to Python dict

with open("kalshi_events.json", "w") as f:
    json.dump(data, f, indent=2)

print("Saved to kalshi_events.json")
