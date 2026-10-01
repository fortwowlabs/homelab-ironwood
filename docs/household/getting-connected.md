# Getting connected

## Quick read

- **On Brandon's Wi-Fi:** nothing to do — just open the apps.
- **Anywhere else** (your place, out and about, mobile data): open the
  **Tailscale** app and make sure it's on, then use the apps as normal.
- **The TV at your place** needs Tailscale on too.
- Your username everywhere is **valerie**. Lost a password? Ask Brandon.

## What Tailscale is

Tailscale is a free app that gives your phone or TV a private, safe link
back to the home server, so everything works the same as it does on
Brandon's Wi-Fi.

Without it, nothing in this guide loads when you're away from Brandon's —
not even this page. Leave it on whenever you're away.

## How to tell it's on

Open the **Tailscale** app. It shows whether you're connected. If there's
a switch, it should be on. That's all you need to check.

## On your iPhone

Tailscale is already set up on your iPhone — you did it a while ago.

1. Open **Tailscale**.
2. If it asks you to sign in, use the same account you signed in with
   before. Not sure which one? Ask Brandon.
3. Turn it on, and check it shows you're connected.

If it isn't on your phone any more, install **Tailscale** from the App
Store and do the steps above.

## On Android

1. Install **Tailscale** from the Play Store.
2. Open it and sign in with the account Brandon invited you with.
3. Turn it on, and check it shows you're connected.

## On the TV at your place

That TV needs Tailscale on before Jellyfin will load.

1. On the TV, open the **Tailscale** app (it's in the TV's list of apps).
2. Check it shows it's connected. If not, turn it on.
3. Now open Jellyfin.

It's worth checking again after the TV has been unplugged or restarted.

## On the TV at Brandon's

Nothing to do — it's on his Wi-Fi.

## Your apps

Search for these names in the App Store (iPhone) or Play Store
(Android).

| For | iPhone | Android | TV |
| --- | --- | --- | --- |
| Away from home | Tailscale | Tailscale | Tailscale (TV at your place) |
| Movies and TV | Swiftfin | Jellyfin | Jellyfin for Android TV |
| Books and audiobooks | Still: for Audiobookshelf | Audiobookshelf | — |
| Reading (optional) | Readest | Readest | — |

Each app asks for a **server address** the first time. Type it exactly:

| App | Server address |
| --- | --- |
| Swiftfin or Jellyfin | `https://jellyfin.fortwow.dev` |
| Still or Audiobookshelf | `https://abs.fortwow.dev` |

In Jellyfin and Swiftfin the server then shows up by name, **Fort Wow**,
instead of the address.

Asking for movies ([Seerr](https://seerr.fortwow.dev)) and finding books
([Shelfmark](https://shelfmark.fortwow.dev)) have no app — they open in
your phone's web browser.

## Your accounts

You sign in with the same username everywhere: **valerie**. Type it in —
it won't appear in a list to tap. Brandon gave you the password in
person. If you've lost it, ask him — he can reset it.

## Keep this guide on your home screen

Open [guide.fortwow.dev](https://guide.fortwow.dev) in your browser, then:

- **iPhone (Safari):** tap the Share button (a square with an arrow
  pointing up — on newer iPhones it may be inside the **•••** menu), then
  **Add to Home Screen**.
- **Android (Chrome):** tap the **⋮** menu, then the option to add it to
  your home screen.

You can do the same for Seerr and Shelfmark, so they sit next to your
apps. Away from Brandon's they still need Tailscale on.

_Last checked: not yet._
