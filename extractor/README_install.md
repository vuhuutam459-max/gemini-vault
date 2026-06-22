# Chatrove Extractor — Guide

## Option 1: Tampermonkey (recommended)

1. Install the **Tampermonkey** extension in Chrome / Yandex Browser:
   - [Chrome Web Store](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)

2. Click the Tampermonkey icon → **"Create a new script"**

3. Delete everything and paste the code from `gemini_extractor.user.js`

4. Press **Ctrl+S** to save

5. Go to https://gemini.google.com/

6. A **Chatrove** panel appears in the bottom-right corner

7. Click **"Export all chats"**

8. The JSON file is downloaded to your Downloads folder

## Option 2: Console Script (for a one-off dump)

1. Open https://gemini.google.com/

2. Make sure the chat sidebar is open and loaded

3. Press **F12** → the **Console** tab

4. Copy the WHOLE code from `gemini_extractor_console.js`

5. Paste it into the console and press **Enter**

6. The export starts automatically. Progress is shown in the console.

7. To cancel, type: `window.__geminiVaultCancel = true`

## What to do with the JSON file

1. Copy the file into the `Chatrove/Source_Accounts/` folder

2. Run the processor:
   ```
   cd Chatrove
   python processor/parse_and_index.py
   ```

3. Start the viewer:
   ```
   python viewer/serve.py
   ```

4. To export to Obsidian:
   ```
   python processor/export_obsidian.py
   ```

## Multiple accounts

For each account:
1. Sign in to the desired Google account
2. Open Gemini
3. Run the extractor
4. The file will contain the account email in its metadata

You can drop all files into `Source_Accounts/` — the processor
will automatically organize the data by account and deduplicate it.

## Privacy

- All data is processed **locally** in your browser
- No data is **sent** to external servers
- The JSON file contains your chats — treat it as confidential data
