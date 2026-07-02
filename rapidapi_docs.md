# Reddit Scraper & Insights API - Documentation

Welcome to the **Reddit Scraper & Insights API**! This high-performance API is designed to retrieve posts and deeply nested comments directly from Reddit with high reliability and clean output structures.

---

## 🌟 Key Features

* **High Reliability:** Optimized to provide consistent and uninterrupted data access.
* **Deep Nested Comments (`include_replies`):** Retrieve the full conversation tree of a post, flattening replies with clear relationship markers (`is_reply: true`).
* **On-Demand Deep Scrape (`load_more`):** Automatically fetches deeper hidden comments ("Load more replies") for comprehensive data gathering (restricted to **Pro** & **Ultra** subscription tiers).
* **NSFW Content Access:** Fully supports retrieving content from age-restricted subreddits.
* **Modern Clean Output:** JSON structures formatted for immediate ingestion by databases, data pipelines, or LLMs. Now with advanced content metrics (timestamps, upvote ratios, OP markers, controversial flags, and bot filtering).

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
  * `include_nsfw` (boolean, Optional): Whether to include age-restricted (over 18) posts. Default: `true`.
  * `filter_pinned` (boolean, Optional): Whether to automatically filter out announcement posts pinned by moderators. Default: `true`.

#### Example Request:
```http
GET /v1/subreddit-posts?target=subreddit_name&sort=hot&limit=1&include_nsfw=false HTTP/1.1
Host: api-provider.rapidapi.com
```

#### Example Response (200 OK):
```json
{
  "meta": {
    "target_subreddit": "subreddit_name",
    "scraped_url": "https://www.reddit.com/r/subreddit_name/hot/",
    "post_count": 1,
    "method_used": "standard",
    "execution_time_ms": 620
  },
  "data": [
    {
      "id": "1ulb60k",
      "title": "Example Post Title",
      "description": "Optional body text of the post...",
      "author": "redditor_username",
      "created_utc": 1782976631.0,
      "upvotes": 1250,
      "upvote_ratio": 0.86,
      "comment_count": 85,
      "is_pinned": false,
      "is_nsfw": false,
      "image_url": "https://preview.redd.it/example.jpg",
      "video_url": null,
      "post_url": "https://www.reddit.com/r/subreddit_name/comments/1ulb60k/example_post_title/"
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
  * `load_more` (boolean, Optional): If `true`, recursively queries to fetch hidden comment branches (corresponds to loading deeper replies). Requires `include_replies=true` and a **Pro/Ultra** plan. Default: `false`.
  * `filter_bots` (boolean, Optional): If `true`, automatically filters out system/automated comments (e.g. `AutoModerator`). Default: `true`.

#### Example Request:
```http
GET /v1/post-comments?post_url=https://www.reddit.com/r/subreddit_name/comments/12345/example_post/&sort=top&limit=5&include_replies=true&filter_bots=true HTTP/1.1
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
    "method_used": "standard",
    "execution_time_ms": 1120
  },
  "data": [
    {
      "id": "ov2p2fc",
      "parent_id": "t3_12345",
      "author": "author_username",
      "comment_text": "This is a root-level comment.",
      "created_utc": 1782977464.0,
      "score": 420,
      "upvotes": 420,
      "is_submitter": false,
      "is_moderator": false,
      "is_controversial": false,
      "is_reply": false
    },
    {
      "id": "ov2p9xy",
      "parent_id": "t1_ov2p2fc",
      "author": "replying_user",
      "comment_text": "This is a direct reply to the root-level comment above.",
      "created_utc": 1782977500.0,
      "score": 35,
      "upvotes": 35,
      "is_submitter": true,
      "is_moderator": false,
      "is_controversial": false,
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
* **HTTP 500 Internal Server Error:** General request processing errors (e.g., target subreddit is private or invalid).

#### Subscription Restriction Response (403 Forbidden):
```json
{
  "detail": "The 'load_more' feature is restricted to Pro and Ultra plans. Please upgrade your subscription."
}
```

#### Request Error Response (500 Internal Server Error):
```json
{
  "detail": {
    "error": "Request failed",
    "message": "Subreddit not found, banned, or private."
  }
}
```
