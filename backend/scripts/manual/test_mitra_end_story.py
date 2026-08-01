import requests
import os
import json

def test():
    # Fetch from env just like rest_client
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkYXRhIjp7ImlkIjoxMzU1LCJuYW1lIjoiUHJpeWFua2EgUHJhZGVlcCIsInNlc3Npb25faWQiOjEyNTQ3LCJvcmdhbml6YXRpb25faWRzIjpbIjYyIl0sIm9yZ2FuaXphdGlvbl9jb2RlcyI6WyJzb3QiXSwidGVuYW50X2NvZGUiOiJzaGlrc2hhbG9rYW0iLCJvcmdhbml6YXRpb25zIjpbeyJpZCI6NjIsIm5hbWUiOiJTb1QiLCJjb2RlIjoic290IiwiZGVzY3JpcHRpb24iOiJTaGlrc2hhTG9rYW0gaXMgc3RyaXZpbmcgdG8gY3JlYXRlIGEgbmF0aW9uYWwgY29udmVyc2F0aW9uIGFib3V0IGVkdWNhdGlvbiBsZWFkZXJzaGlwLXRoZSB3aGF0LCB3aHkgYW5kIGhvdyBvZiBpdC4iLCJzdGF0dXMiOiJBQ1RJVkUiLCJyZWxhdGVkX29yZ3MiOltdLCJ0ZW5hbnRfY29kZSI6InNoaWtzaGFsb2thbSIsIm1ldGEiOm51bGwsImNyZWF0ZWRfYnkiOjEsInVwZGF0ZWRfYnkiOm51bGwsInJvbGVzIjpbeyJpZCI6MjMsInRpdGxlIjoibWVudGVlIiwibGFiZWwiOiJtZW50ZWUiLCJ1c2VyX3R5cGUiOjAsInN0YXR1cyI6IkFDVElWRSIsIm9yZ2FuaXphdGlvbl9pZCI6MTAsInZpc2liaWxpdHkiOiJQVUJMSUMiLCJ0ZW5hbnRfY29kZSI6InNoaWtzaGFsb2thbSIsInRyYW5zbGF0aW9ucyI6bnVsbH0seyJpZCI6NDIsInRpdGxlIjoiY3JlYXRvciIsImxhYmVsIjoiQ3JlYXRvciIsInVzZXJfdHlwZSI6MCwic3RhdHVzIjoiQUNUSVZFIiwib3JnYW5pemF0aW9uX2lkIjoxMCwidmlzaWJpbGl0eSI6IlBVQkxJQyIsInRlbmFudF9jb2RlIjoic2hpa3NoYWxva2FtIiwidHJhbnNsYXRpb25zIjpudWxsfSx7ImlkIjo0NiwidGl0bGUiOiJwcm9ncmFtX2Rlc2lnbmVyIiwibGFiZWwiOiJQcm9ncmFtIERlc2lnbmVyIiwidXNlcl90eXBlIjowLCJzdGF0dXMiOiJBQ1RJVkUiLCJvcmdhbml6YXRpb25faWQiOjEwLCJ2aXNpYmlsaXR5IjoiUFVCTElDIiwidGVuYW50X2NvZGUiOiJzaGlrc2hhbG9rYW0iLCJ0cmFuc2xhdGlvbnMiOm51bGx9XX1dfSwiaWF0IjoxNzg1MjQyNTY5LCJleHAiOjE3ODU4NDczNjl9.misOFt1cSN-smsRVy8-VzuBvbF9DO-VDVDKyEZhR6g0"
    
    url = "https://qa-mohini.shikshalokam.org/api/end-story/v2/"
    headers = {
        "Origin": "https://qa-elevate-mitra.shikshalokam.org",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Generate a dummy profile and session to be safe, or just use dummy strings and see what exception we get
    # Let's hit /api/generate-session/
    session = requests.Session()
    session.headers.update(headers)
    
    res1 = session.post("https://qa-mohini.shikshalokam.org/api/profile/", json={
        "email": "1355@shikshalokam",
        "latest_flow_used": "guest-discussion",
        "company": "shikshalokamstaging"
    })
    print("profile:", res1.status_code, res1.text)
    profile_id = res1.json().get("id") or res1.json().get("profileid")

    res2 = session.get("https://qa-mohini.shikshalokam.org/api/generate-session/")
    print("session:", res2.status_code, res2.text)
    session_id = res2.json().get("sessionid")

    print(f"Calling end-story/v2 with session={session_id}, profile_id={profile_id}")
    res3 = session.post(url, json={
        "session": session_id,
        "profile_id": profile_id,
        "stage": "COMPLETED",
        "flow": "guest-discussion",
        "language": "en"
    })
    print("end-story/v2:", res3.status_code)
    try:
        print(json.dumps(res3.json(), indent=2))
    except:
        print(res3.text)

if __name__ == "__main__":
    test()
