
---

# Development Concept: Reddit Data Extraction API

## 1. Architektur & Tech-Stack

* **Framework:** Python mit FastAPI (für schnelles, asynchrones API-Routing).
* **Scraping-Engine:** Playwright (Headless Chromium) kombiniert mit `playwright-stealth` zur Bot-Verschleierung.
* **Proxy-Readiness:** Die Funktion zum Starten des Playwright-Browsers wird als isolierte Methode geschrieben (`launch_browser()`), damit später per Parameter einfach ein Proxy-Server (IP, Port, Username, Passwort) übergeben werden kann, ohne die Scraping-Logik anfassen zu müssen.

---

## 2. Endpoint 1: Subreddit Posts extrahieren

**Route:** `GET /v1/subreddit-posts` oder `POST /v1/subreddit-posts`

### Input Parameter (Query oder JSON-Body)

| Parameter | Typ | Pflicht | Beschreibung | Erlaubte Werte / Logik |
| --- | --- | --- | --- | --- |
| `target` | String | **Ja** | Der Subreddit. Akzeptiert den reinen Namen (z.B. `beziehungen`) oder die volle URL. | `beziehungen` oder `https://www.reddit.com/r/...` |
| `sort` | String | Nein | Sortiermethode des Subreddits. (Default: `hot`). | `best`, `hot`, `new`, `top`, `rising` |
| `timeframe` | String | Nein | Zeitraum-Filter. **Wird nur ignoriert, wenn `sort` nicht `top` ist.** | `hour`, `day`, `week`, `month`, `year`, `all` |
| `limit` | Integer | Nein | Anzahl der zu extrahierenden Posts. (Default: `10`). | `1` bis `50` |

### Interne Routing-Logik (URL-Builder)

Das Skript prüft den Input `target`:

1. Enthält der String `http`? -> Nutze die Base-URL und schneide eventuelle Parameter ab.
2. Kein `http`? -> Konstruiere Base-URL: `https://www.reddit.com/r/{target}/`
3. Hänge `sort` an: `.../r/beziehungen/{sort}/`
4. Wenn `sort == top`, hänge Query an: `.../top/?t={timeframe}`

### JSON Output (Beispiel)

```json
{
  "meta": {
    "target_subreddit": "beziehungen",
    "scraped_url": "https://www.reddit.com/r/beziehungen/top/?t=week",
    "post_count": 5
  },
  "data": [
    {
      "title": "Freundin (W29) will nur als Hauptmieterin...",
      "description": "Hallo zusammen, meine Freundin und ich wollen zusammenziehen...",
      "post_url": "https://www.reddit.com/r/beziehungen/comments/1ugxl6r/...",
      "upvotes": 452,
      "comment_count": 128,
      "author": "Throwaway123"
    }
  ]
}

```

---

## 3. Endpoint 2: Post-Kommentare extrahieren

**Route:** `GET /v1/post-comments` oder `POST /v1/post-comments`

### Input Parameter

| Parameter | Typ | Pflicht | Beschreibung | Erlaubte Werte / Logik |
| --- | --- | --- | --- | --- |
| `post_url` | String | **Ja** | Die exakte URL des Reddit-Posts. | Gültige Reddit-URL. |
| `sort` | String | Nein | Sortiermethode der Kommentare. (Default: `confidence`). | `confidence` (best), `top`, `new`, `controversial`, `old`, `qa` |
| `limit` | Integer | Nein | Anzahl der Root-Kommentare, die extrahiert werden sollen. | `1` bis `50` |

### Interne Routing-Logik (URL-Builder)

Das Skript nimmt die `post_url` und bereinigt sie von bestehenden Query-Parametern (alles nach einem eventuellen `?` wird abgeschnitten). Danach wird der Sortier-Parameter als Query angehängt:
`{post_url_clean}?sort={sort}`

### JSON Output (Beispiel)

```json
{
  "meta": {
    "scraped_url": "https://www.reddit.com/r/beziehungen/comments/1uggj5m/familienurlaub_der_blanke_horror/?sort=confidence",
    "comment_count": 5
  },
  "data": [
    {
      "comment_text": "Ganz ehrlich, da hätte ich direkt im Hotel wieder umgedreht. Das Verhalten geht gar nicht.",
      "upvotes": 1204,
      "author": "RealTalker99",
      "is_reply": false
    },
    {
      "comment_text": "Habt ihr vorher nicht darüber gesprochen, wie die Kosten aufgeteilt werden?",
      "upvotes": 850,
      "author": "QuestionMark4",
      "is_reply": false
    }
  ]
}

```

---

## 4. Entwicklungs-Phasen & Proxy-Vorbereitung

**Phase 1: Lokaler Build & DOM-Analyse**

* FastAPI Grundgerüst aufbauen.
* Playwright Skript schreiben. Der wichtigste und schwierigste Teil in Phase 1 wird das Identifizieren der korrekten HTML-Selektoren (CSS-Classes oder XPaths) auf Reddit sein. *Tipp: Reddit nutzt stark dynamische Klassen. Suche am besten nach standardisierten Attributen wie `data-testid="post-title"` oder ähnlichen stabilen Markern.*
* Testen der Skripte ohne Proxys von deiner lokalen Maschine aus.

**Phase 2: KVM Deployment & Stealth-Modus**

* Sobald die Selektoren stabil Daten liefern, packst du alles in einen Docker-Container.
* An dieser Stelle wird in der `launch_browser()` Funktion das Argument für den Proxy ergänzt (z.B. über Umgebungsvariablen `.env`, damit deine Proxy-Passwörter nicht fest im Code stehen).