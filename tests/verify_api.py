import time
import requests
import sys

BASE_URL = "http://localhost:8100/api/v1"

def wait_for_health():
    print("Waiting for service to be healthy...")
    for _ in range(300): # Wait up to 300 seconds (5 mins) for model download
        try:
            r = requests.get("http://localhost:8100/health")
            if r.status_code == 200:
                print("Service is healthy!")
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
    print("Service failed to start.")
    return False

def test_ingest():
    print("\nTesting Ingest...")
    # Use a small page or the main page
    payload = {
        "url": "https://cloud.tencent.com/document/product/213/495",
        "max_pages": 1 # Limit to 1 page for quick test
    }
    r = requests.post(f"{BASE_URL}/ingest", json=payload)
    print(f"Status: {r.status_code}")
    print(f"Response: {r.json()}")
    if r.status_code == 200:
        print("Ingest task started.")
        return True
    return False

def test_chat():
    print("\nTesting Chat...")
    # Give some time for crawler to finish (it's async)
    print("Waiting 10s for crawler to index some data...")
    time.sleep(10)
    
    payload = {
        "query": "云服务器 CVM 是什么？"
    }
    r = requests.post(f"{BASE_URL}/chat", json=payload)
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Answer: {data.get('answer')}")
        print(f"Sources: {data.get('sources')}")
        return True
    else:
        print(f"Error: {r.text}")
        return False

if __name__ == "__main__":
    if not wait_for_health():
        sys.exit(1)
    
    if not test_ingest():
        sys.exit(1)
        
    if not test_chat():
        sys.exit(1)
    
    print("\nAll verification tests passed!")
