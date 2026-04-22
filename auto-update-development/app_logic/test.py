import requests

BASE = "https://api.os.uk/downloads/v1/products"

products = requests.get(BASE).json()

for p in products:
    print(f"{p['id']} | {p['name']} | latest version: {p.get('version', 'N/A')}")