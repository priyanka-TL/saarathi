import concurrent.futures
import requests
import uuid
import time
import os

def test_concurrency():
    url = "http://127.0.0.1:5000/api/chat"
    
    headers = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:5000",
        "Referer": "http://127.0.0.1:5000/"
    }
    # Read token if set
    token = os.getenv("SAARTHI_USER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    conv_id = str(uuid.uuid4())
    payload = {
        "message": "Hello, I want to record a story",
        "agent_key": "record_stories",
        "conversation_id": conv_id
    }
    
    print(f"Creating new session {conv_id}...")
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        print(f"Failed to create session: {resp.status_code} {resp.text}")
        return

    print("Session created. Now sending 5 concurrent requests for the next turn...")
    
    next_payload = {
        "message": "No",
        "agent_key": "record_stories",
        "conversation_id": conv_id
    }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(requests.post, url, json=next_payload, headers=headers) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            try:
                r = f.result()
                results.append(r.status_code)
                print(f"Response status: {r.status_code}")
                if r.status_code != 200:
                    print(f"Error body: {r.json()}")
            except Exception as e:
                print(f"Request threw an exception: {e}")

    status_counts = {}
    for code in results:
        status_counts[code] = status_counts.get(code, 0) + 1
        
    print("\nResults:")
    for code, count in status_counts.items():
        print(f"HTTP {code}: {count}")
        
    assert status_counts.get(200, 0) == 1, "Expected exactly one 200 OK"
    assert status_counts.get(429, 0) == 4, "Expected exactly four 429 Too Many Requests"
    print("Test passed: concurrency successfully managed!")

if __name__ == "__main__":
    test_concurrency()
