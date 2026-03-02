---
name: reddit
description: Scrape posts, comments, and data from Reddit using Camoufox stealth browser.
homepage: https://www.reddit.com
metadata: {"nanobot":{"emoji":"🟠"}}
---

# Reddit Scraper

This skill enables scraping posts, comments, and other data from Reddit.com using the Camoufox anti-detect browser to bypass bot protection and Cloudflare challenges.

## When to use

Use this skill when you need to:

- Extract posts, titles, upvotes, and comments from Reddit subreddits
- Search for posts on Reddit and retrieve listings
- Get structured data from Reddit pages or specific posts
- Perform multi-step workflows like browsing subreddits, navigating to posts, and extracting details

## Prerequisites

- Camoufox must be installed: `pip install camoufox[geoip]`
- Python environment with nanobot and Camoufox tools loaded
- Basic knowledge of CSS selectors for targeting elements on Reddit pages

## How to use

### 1. Fetch a Subreddit Page

Use the `camoufox_fetch` tool to load a Reddit subreddit page and extract content.

Example for a subreddit:

```json
{
  "url": "https://www.reddit.com/r/International/",
  "extractMode": "html",
  "waitSeconds": 3,
  "sessionId": "reddit_session"
}
```

This loads the page, waits for JS rendering, and returns the HTML content.

### 2. Extract Post Data

Use the `camoufox_script` tool to run JavaScript on the loaded page and extract structured data.

Example script to get post titles, upvotes, and links:

```javascript
(() => {
  const posts = [];
  document.querySelectorAll('[data-testid="post-container"]').forEach(post => {
    const title = post.querySelector('[data-adclicklocation="title"]')?.textContent?.trim();
    const upvotes = post.querySelector('[data-testid="upvote-button"] + div')?.textContent?.trim();
    const link = post.querySelector('a[data-testid="post-title"]')?.href;
    if (title) {
      posts.push({ title, upvotes, link });
    }
  });
  return posts.slice(0, 10);  // Top 10 posts
})()
```

Call with:

```json
{
  "script": "(() => { const posts = []; document.querySelectorAll('[data-testid=\"post-container\"]').forEach(post => { const title = post.querySelector('[data-adclicklocation=\"title\"]')?.textContent?.trim(); const upvotes = post.querySelector('[data-testid=\"upvote-button\"] + div')?.textContent?.trim(); const link = post.querySelector('a[data-testid=\"post-title\"]')?.href; if (title) { posts.push({ title, upvotes, link }); } }); return posts.slice(0, 10); })()",
  "sessionId": "reddit_session",
  "waitSeconds": 1
}
```

### 3. Navigate to a Specific Post

Use `camoufox_action` to click on a post link and navigate to the post page.

Example action to click on the first post:

```json
{
  "actions": [
    {
      "action": "click",
      "selector": '[data-testid="post-container"] a[data-testid="post-title"]',
      "waitForSelector": '[data-testid="post-content"]'
    }
  ],
  "sessionId": "reddit_session",
  "extractAfter": true,
  "extractMode": "markdown"
}
```

### 4. Extract Comments from a Post

Once on a post page, use `camoufox_script` to extract comments.

Example script:

```javascript
(() => {
  const comments = [];
  document.querySelectorAll('[data-testid="comment"]').forEach(comment => {
    const author = comment.querySelector('[data-testid="comment-author"]')?.textContent?.trim();
    const text = comment.querySelector('[data-testid="comment-content"] p')?.textContent?.trim();
    const upvotes = comment.querySelector('[data-testid="comment-upvote-button"] + div')?.textContent?.trim();
    if (author && text) {
      comments.push({ author, text, upvotes });
    }
  });
  return comments.slice(0, 20);  // Top 20 comments
})()
```

### 5. Search for Posts

Navigate to a Reddit search URL and extract search results.

First, use `camoufox_fetch` or `camoufox_action` to load the search page:

```json
{
  "url": "https://www.reddit.com/search/?q=laptop",
  "extractMode": "html",
  "waitSeconds": 3,
  "sessionId": "reddit_session"
}
```

Then, extract results similarly to subreddit posts.

### 6. Multi-Step Workflows

Use `sessionId` to maintain browser state across calls. For example:

- Start a session and load a subreddit
- Click on a post using `camoufox_action`
- Extract post details and comments

### 7. Handle Anti-Bot Measures

- Always use `waitSeconds` to allow page loading
- Use `sessionId` for persistent sessions to mimic human behavior
- If blocked, try different proxies or geoip settings in Camoufox
- Reddit may require login for some content; use actions to log in if needed

## Notes

- Reddit frequently changes its page structure; update selectors as needed
- Respect Reddit's terms of service, robots.txt, and rate limits
- For large-scale scraping, consider using proxies and rotating user agents
- Camoufox sessions auto-expire after 5 minutes of inactivity
- Use `camoufox_session` tool to manage sessions if needed

## Examples

### Scrape posts from a subreddit

1. Fetch subreddit page with `camoufox_fetch`
2. Extract posts with `camoufox_script`

### Scrape comments from a specific post

1. Fetch post page
2. Extract comments with script

### Search and scrape posts

1. Fetch search page
2. Extract results with script
3. Navigate to individual posts if needed

This skill leverages the full power of Camoufox for reliable Reddit data extraction.