from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_ID = ""
CLIENT_SECRET = ""
client_config = {"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost:8080/"]}}
flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=8080, prompt=\'consent\', access_type=\'offline\')
print(f"YT_REFRESH_TOKEN = {creds.refresh_token}")