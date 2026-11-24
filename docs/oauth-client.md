# Create a Google OAuth desktop client

[Leer en español](oauth-client.es.md) · [Back to the README](../README.md)

nimbo uses a Google OAuth desktop client that you create for your own account.
You need a Google Cloud project with the Google Drive API enabled. A billing
account and payment card are unnecessary for this setup.

## 1. Create a project and enable the Drive API

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create
   a project. Choose it in the project selector.
2. Go to **APIs & Services → Library**, search for **Google Drive API**, and
   choose **Enable**.

## 2. Configure and publish the consent screen

1. Open **Google Auth Platform** and select **Get started** if prompted. In
   **Branding**, set an app name you will recognize and supply your user support
   and developer contact email addresses.
2. Choose **External** in **Audience**. In **Data Access**, add the Google Drive
   scope `https://www.googleapis.com/auth/drive`. nimbo uses it to manage the
   mirror in an existing folder and to work with items the OAuth client did not
   create. See the [privacy policy](../site/privacy.html).
3. In **Branding**, supply the **Application home page** URL and **Application
   privacy policy** URL on an **authorized domain**. A free GitHub Pages site
   from a public repository works. Publish an index page that explains the app
   and links to its privacy policy, publish the policy at its own HTTPS URL,
   and add the site's domain under **Authorized domains**. Enter the two
   published URLs in the corresponding fields.
4. In **Audience**, choose **Publish app** and check that **Publishing status**
   shows **In production**. In **Testing**, Google expires refresh tokens
   after seven days for apps requesting the Drive scope, which interrupts an
   unattended backup. If you already connected while the app was in Testing,
   run `backup conectar` after publishing to authorize again.

Publishing changes the audience status. Verification is a separate Google
review. A personal client can show an unverified-app warning even after you
publish it.

## 3. Create the Desktop client

In **Google Auth Platform → Clients**, choose **Create client**, set
**Application type** to **Desktop app**, name it, and create it. The CLI handles
the browser's redirect back to your computer.

Download the client JSON and keep it private. Never commit or share the file.
Replace `PATH` with the path to the downloaded file.

```sh
chmod 600 "PATH"
backup instalar --client-json "PATH"
```

Alternatively, run `backup instalar` and enter the client ID and client secret
at its prompts. The wizard hides the secret as you type. nimbo saves desktop
client credentials in a mode-0600 configuration file and puts the refresh
token in your Linux keyring.

## 4. Authorize your Google account

When the wizard opens the browser, select the account that can write to the
destination folder and review the requested Drive access.

If Google shows **Google hasn't verified this app**, it means that the OAuth
client and its requested scope have not passed Google's verification review.
Continue only when you recognize the app name as the client you just created.
Choose **Advanced**, then **Go to your app's name (unsafe)**, and grant access.
If Google blocks that option, check the account, the project's audience and
publishing status, and any organization policy on OAuth access.

Return to the terminal to confirm the account and destination and finish setup.
You can revoke access from
[Google Account permissions](https://myaccount.google.com/permissions).
