# Reddit Scraper & Insights API - Documentation

Welcome to the **Reddit Scraper & Insights API**! This high-performance scraping engine is designed to retrieve posts and deeply nested comments directly from Reddit. By using a hybrid JSON-extraction and stealth-browser architecture, it ensures maximum stability, speed, and bypasses anti-bot blocks seamlessly.

---

## 🌟 Key Features

* **Hybrid Extraction Engine:** Automatically attempts a lightning-fast JSON extraction first, falling back to a headless Stealth browser session if Reddit rate-limits or blocks the connection.
* **Deep Nested Comments (`include_replies`):** Retrieve the full conversation tree of a post, flattening replies with clear relationship markers (`is_reply: true`).
* **On-Demand Deep Scrape (`load_more`):** Automatically triggers background recursive actions to click and fetch deeper hidden comments ("Load more replies") for comprehensive data gathering (restricted to **Pro** & **Ultra** subscription tiers).
* **NSFW & Private Content Access:** Leverages active credential pools to safely parse age-restricted subreddits.
* **Modern Clean Output:** JSON structures formatted for immediate ingestion by databases, data pipelines, or LLMs.

---

## 🛠️ API Endpoint Reference

### 1. Get Subreddit Posts
Extracts a list of posts from a specific Subreddit or custom feed URL.

* **Endpoint:** `GET /v1/subreddit-posts`
* **Query Parameters:**
  * `target` (string, Required): The name of the Subreddit (e.g., `technology`) or the full Subreddit URL (e.g., `https://www.reddit.com/r/technology/rising/`). Sorting categories in paths will override the `sort` query parameter automatically.
  * `sort` (string, Optional): The sorting method. Allowed: `hot` (default), `new`, `top`, `rising`.
  * `timeframe` (string, Optional): Time range filter (only applicable when `sort=top`). Allowed: `hour`, `day` (default), `week`, `month`, `year`, `all`.
  * `limit` (integer, Optional): Maximum number of posts to return. Range: `1` to `100`. Default: `5`.

#### Example Request:
```http
GET /v1/subreddit-posts?target=subreddit_name&sort=hot&limit=1 HTTP/1.1
Host: api-provider.rapidapi.com
```

#### Example Response (200 OK):
```json
{
  "meta": {
    "target_subreddit": "subreddit_name",
    "scraped_url": "https://www.reddit.com/r/subreddit_name/hot/",
    "post_count": 1,
    "method_used": "json",
    "execution_time_ms": 620
  },
  "data": [
    {
      "title": "Example Post Title",
      "description": "Optional body text of the post...",
      "image_url": "https://preview.redd.it/example.jpg",
      "video_url": null,
      "post_url": "https://www.reddit.com/r/subreddit_name/comments/12345/example_post_title/",
      "upvotes": 1250,
      "comment_count": 85,
      "author": "redditor_username"
    }
  ]
}
```

---

### 2. Get Post Comments
Extracts deeply nested comments and reply trees from a specific Reddit post URL.

* **Endpoint:** `GET /v1/post-comments`
* **Query Parameters:**
  * `post_url` (string, Required): The full URL of the Reddit post.
  * `sort` (string, Optional): Comment sorting. Allowed: `confidence` (default - "Best"), `top`, `new`, `controversial`, `old`, `qa`.
  * `limit` (integer, Optional): Maximum number of **root-level (main) comments** to extract. Range: `1` to `100`. Default: `10`.
  * `include_replies` (boolean, Optional): If `true`, returns replies to the comments. Default: `false`.
  * `load_more` (boolean, Optional): If `true`, recursively queries Reddit to fetch hidden comment branches (corresponds to clicking "10 more replies" on the web). Requires `include_replies=true` and a **Pro/Ultra** plan. Default: `false`.

#### Example Request:
```http
GET /v1/post-comments?post_url=https://www.reddit.com/r/subreddit_name/comments/12345/example_post/&sort=top&limit=5&include_replies=true HTTP/1.1
Host: api-provider.rapidapi.com
```

#### Example Response (200 OK):
```json
{
  "meta": {
    "scraped_url": "https://www.reddit.com/r/subreddit_name/comments/12345/example_post/?sort=top",
    "comment_count": 2,
    "include_replies": true,
    "load_more": false,
    "method_used": "json",
    "execution_time_ms": 1120
  },
  "data": [
    {
      "comment_text": "This is a root-level comment.",
      "upvotes": 420,
      "author": "author_username",
      "is_reply": false
    },
    {
      "comment_text": "This is a direct reply to the root-level comment above.",
      "upvotes": 35,
      "author": "replying_user",
      "is_reply": true
    }
  ]
}
```

---

## 🚨 Error Handling

The API returns standard HTTP status codes. In case of validation issues or restricted plan features, detailed JSON error messages are returned:

* **HTTP 400 Bad Request:** Missing required parameters or invalid parameter ranges (e.g. limit out of bounds).
* **HTTP 403 Forbidden:** Plan restrictions (e.g., requesting `load_more=true` on a Basic plan tier).
* **HTTP 500 Internal Server Error:** General scraping issues (e.g., target subreddit is private or has been banned).

#### Subscription Restriction Response (403 Forbidden):
```json
{
  "detail": "The 'load_more' feature is restricted to Pro and Ultra plans. Please upgrade your subscription."
}
```

#### Scraping Error Response (500 Internal Server Error):
```json
{
  "detail": {
    "error": "Scraping error",
    "message": "Scraping error: Subreddit not found, banned, or private."
  }
}
```
