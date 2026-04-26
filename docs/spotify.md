# Spotify on SoundTouch

As always, requires a Premium Spotify account to work.

## Two Different Spotify Systems

There are two completely separate ways Spotify works on a SoundTouch speaker. This is a common source of confusion.

### 1. Spotify Connect (Always Works)

- The speaker advertises itself as a Spotify Connect device on your local network
- Open the Spotify app on your phone or computer, tap the speaker/device icon, and select your SoundTouch speaker
- Audio streams directly from Spotify's CDN to the speaker
- Doesn't use Soundcork in any way.

## Spotify managed by speakers or Soundtouch software using Soundcork

Soundcork allows users to maintain Spotify presets, and continue to play Spotify streams over any Soundtouch-enabled app.

### OAuth Token Intercept (Ongoing Refresh)

The speaker requests a Spotify session from the synthetic Bose APIs (Soundcork), which negotiates for a session token with the Spotify APIs.

Once the speaker has an active Spotify session (from the ZeroConf primer or a previous Spotify Connect cast), it will periodically refresh its token by calling a Bose OAuth endpoint. Soundcork intercepts these requests and returns a valid token.

**Note:** The SoundTouch speakers don't have a separate configuration for the OAuth server. Rather, they take the marge server address and append `oauth` to the end of the first part of the hostname. So for the Bose systems, this this changing `https://streaming.bose.com`  to  `https://streamingoauth.bose.com`. For Soundcork to work with Spotify, it must be available both at a hostname and at an oauth hostname, so `soundcork.local.domain` and `soundcorkoauth.local.domain`.

**How it works:**

1. Speaker sends `POST {oauth_server}/oauth/device/{deviceId}/music/musicprovider/15/token/cs3`
2. Soundcork refreshes the token using the stored Spotify account credentials
3. Returns a fresh access token as JSON
4. The speaker uses this token for continued Spotify playback


## Setup

### Step 1: Register a Spotify App

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
   - **Redirect URI**: `{your-soundcork-url}/mgmt/spotify/callback` (e.g., `https://soundcork.local:8000/mgmt/spotify/callback`)
   - **APIs used**: Web API
3. Note the **Client ID** and **Client Secret**

### Step 2: Configure Soundcork

**NOTE: this configuration may change in the future**

Set the environment variables:

```bash
SPOTIFY_CLIENT_ID=your-client-id
SPOTIFY_CLIENT_SECRET=your-client-secret
```

### Step 3: Link Your Spotify Account

**Note: in the future we will add a web UI for account management**

Using the management API:

```bash
# Start the OAuth flow
curl -u admin:password https://your-soundcork/mgmt/spotify/auth/init

# Open the returned URL in your browser, authorize, then complete:
curl -u admin:password "https://your-soundcork/mgmt/spotify/auth/callback?code=AUTH_CODE"

# Verify the account is linked
curl -u admin:password https://your-soundcork/mgmt/spotify/accounts
```

### Step 4: Add refresh token to source configuration

**TODO**

The refresh token from Spotify must be added to the Source configuration. In the near future a web UI will do this for you. For now, you can edit your Sources.xml file directly.

```
   <source id="34" secret="{your refresh token, should start with AQ}" secretType="token_version_3">
        <sourceKey type="SPOTIFY" account="{your account id, should be a long alphanumeric string}" />
    </source>
```

After the account setup above, both `secret` and `account` information should be available in `{soundcorkdbdir}/spotify/accounts.json`.

### Step 5: Verify

After linking your Spotify account, press a Spotify preset button on the speaker. If nothing plays, check your server logs.

## Technical Details

### Token Lifecycle

- Spotify access tokens expire after 1 hour (3600 seconds)
- The speaker's firmware requests a new token via the OAuth endpoint before expiry
- Soundcork caches tokens to avoid unnecessary Spotify API calls


### Speaker ZeroConf Endpoint

Each speaker exposes a ZeroConf endpoint on port 8200:
- `GET /zc?action=getInfo` — returns speaker info including `activeUser`, `libraryVersion`
- `POST /zc` with `action=addUser` — sets the active Spotify user

### Alternative Approach: Manual Kick-Start

If you don't want to configure Spotify credentials in soundcork, you can manually prime the speaker by casting one song via the Spotify app (Spotify Connect). This gives the speaker a temporary in-memory session that enables presets. However, you'll need to repeat this after every speaker reboot.

### ZeroConf Primer (Cold Boot Activation)

**NOTE because the automatic token refresh works in soundcork, thie ZeroConf Primer method has been disabled. The code is still available and the documentation is available here as a reference in case issues with the automatic token refresh are found during testing.**

On cold boot, the speaker does **not** request a Spotify token — it only fetches account data, source providers, and streaming tokens. Without an active Spotify session, presets fail silently.

The ZeroConf primer solves this by proactively pushing a fresh Spotify access token to the speaker via the ZeroConf endpoint (port 8200). This is the same mechanism the Spotify desktop app uses when you cast to a speaker.

**When it runs:**
- On speaker boot (`power_on` event), with retry/backoff (5s, 10s, 20s delays)
- Periodically every 45 minutes (tokens expire after 1 hour)
- When a new speaker is first seen via marge requests

**Boot sequence observed:**
```
power_on → bmx/services → media icons → sourceproviders → /full → streaming_token → provider_settings
```
No OAuth token request happens during boot — the ZeroConf primer is what activates Spotify.