# Dein Orga-Bot – Einrichtung in 5 Schritten

**Was der Bot kann**
- 📸 Foto schicken (Flohmarkt-Plakat, Flyer, Screenshot einer Einladung) → er liest Datum, Uhrzeit, Ort und fragt: „Soll ich das eintragen?“ → ✅ → steht im Google Kalender
- ✍️ Geht auch per Text: „Zahnarzt Donnerstag 14 Uhr“
- 🔁 Korrektur einfach hinterherschreiben: „erst ab 10 Uhr“ oder „nur Samstag“
- 📅 `/heute`, `/morgen`, `/woche` – zeigt deine Termine
- ☀️ Jeden Morgen um 7:30 Uhr automatisch: Termine des Tages + die wichtigsten ungelesenen Mails (Newsletter werden aussortiert)
- 🔒 Reagiert nur auf dich

**Dauer:** ca. 30–40 Minuten, einmalig.
**Kosten:** Railway ca. 5 $/Monat, Claude-API ein paar Cent pro Foto (realistisch 1–3 €/Monat).

---

## Schritt 1 – Telegram-Bot anlegen (3 Min.)

1. In Telegram **@BotFather** öffnen → `/newbot`
2. Name eingeben (z. B. „Ewas Orga“) und Benutzername (muss auf `bot` enden, z. B. `ewa_orga_bot`)
3. Du bekommst einen **Token** (sieht aus wie `7123456789:AAH...`) → kopieren und sicher notieren

## Schritt 2 – Claude-API-Key holen (3 Min.)

1. [console.anthropic.com](https://console.anthropic.com) → einloggen/registrieren
2. Unter **Billing** 5–10 € Guthaben aufladen
3. **API Keys** → „Create Key“ → Key (`sk-ant-...`) notieren

## Schritt 3 – Google verbinden (15 Min., der einzige fummelige Teil)

### 3a) Projekt + APIs
1. [console.cloud.google.com](https://console.cloud.google.com) mit **dem Google-Konto, dessen Kalender/Mail du nutzen willst**
2. Oben „Projekt auswählen“ → **Neues Projekt** → Name „Orga-Bot“ → Erstellen
3. Suchleiste: **Google Calendar API** → Aktivieren
4. Suchleiste: **Gmail API** → Aktivieren

### 3b) Zustimmungsbildschirm
1. Menü → **APIs & Dienste → OAuth-Zustimmungsbildschirm** (heißt evtl. „Google Auth Platform“)
2. Typ **Extern**, App-Name „Orga-Bot“, deine Mail als Support- und Kontakt-Mail → Speichern
3. Unter **Zielgruppe / Testnutzer**: deine Gmail-Adresse hinzufügen
4. **Wichtig:** Veröffentlichungsstatus auf **„In Produktion“** stellen („App veröffentlichen“).
   Sonst läuft die Verbindung nach 7 Tagen ab. Eine Prüfung durch Google brauchst du für den Eigengebrauch nicht.

### 3c) Zugangsdaten
1. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID**
2. Anwendungstyp: **Webanwendung**
3. Bei **Autorisierte Weiterleitungs-URIs** eintragen:
   `https://developers.google.com/oauthplayground`
4. Erstellen → **Client-ID** und **Clientschlüssel** notieren

### 3d) Refresh-Token holen (OAuth Playground)
1. [developers.google.com/oauthplayground](https://developers.google.com/oauthplayground) öffnen
2. Rechts oben das **Zahnrad** → Haken bei **„Use your own OAuth credentials“** → Client-ID und Clientschlüssel einfügen
3. Links unten ins Feld „Input your own scopes“ diese zwei Zeilen (mit Leerzeichen getrennt) einfügen:
   ```
   https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.readonly
   ```
4. **Authorize APIs** → mit deinem Google-Konto anmelden.
   Kommt „Google hat diese App nicht überprüft“: auf **Erweitert → Zu Orga-Bot wechseln** klicken (ist deine eigene App) → alles erlauben
5. **Exchange authorization code for tokens** klicken
6. Den **Refresh token** (`1//0...`) kopieren und notieren

> Der Bot darf damit Termine anlegen und Mails **nur lesen** – er kann keine Mails senden oder löschen.

## Schritt 4 – Auf Railway starten (5 Min.)

1. Den Ordner `orga-bot` in ein (privates) **GitHub-Repo** hochladen
   (github.com → New repository → „uploading an existing file“ → alle Dateien reinziehen)
2. [railway.com](https://railway.com) → mit GitHub anmelden → **New Project → Deploy from GitHub repo** → dein Repo wählen
3. Im Service auf **Variables** → diese Werte eintragen:

| Variable | Wert |
|---|---|
| `TELEGRAM_BOT_TOKEN` | aus Schritt 1 |
| `ANTHROPIC_API_KEY` | aus Schritt 2 |
| `GOOGLE_CLIENT_ID` | aus 3c |
| `GOOGLE_CLIENT_SECRET` | aus 3c |
| `GOOGLE_REFRESH_TOKEN` | aus 3d |
| `ALLOWED_USER_ID` | **erst mal leer lassen** |

Optional: `DAILY_TIME` (Standard `07:30`), `CALENDAR_ID` (Standard: dein Hauptkalender).

4. Railway startet den Bot automatisch (unter **Deployments** sollte „Bot läuft.“ im Log stehen).

## Schritt 5 – Bot für dich freischalten (1 Min.)

1. Deinem Bot in Telegram `/start` schreiben
2. Er antwortet mit **„Deine Telegram-ID ist 12345678“**
3. Diese Zahl in Railway als `ALLOWED_USER_ID` eintragen → Railway startet neu
4. Nochmal `/start` → fertig 🎉

**Erster Test:** Foto von einem Plakat schicken → Vorschau prüfen → ✅ Eintragen → im Google Kalender nachsehen.
Danach `/uebersicht` testen, um die Morgenübersicht sofort zu sehen.

---

## Wenn was nicht klappt

| Problem | Lösung |
|---|---|
| Bot antwortet gar nicht | Railway → Deployments → Logs ansehen. Steht da „Fehlende Variablen“, fehlt ein Eintrag. |
| „Kalender konnte nicht geladen werden“ | Refresh-Token falsch kopiert oder Calendar API nicht aktiviert (3a). |
| Ging eine Woche, dann nicht mehr | App steht noch auf „Testen“ → Schritt 3b Punkt 4, danach 3d wiederholen und Token in Railway ersetzen. |
| Termin hat falsches Jahr/Datum | Einfach Korrektur schreiben („ist 2027“) – oder ❌ Verwerfen und mit Hinweis im Foto-Text neu senden. |
| Morgenübersicht zu anderer Zeit | `DAILY_TIME` in Railway ändern, z. B. `06:45`. |
