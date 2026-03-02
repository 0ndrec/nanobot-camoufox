---
name: amazon
description: Scrape product information from Amazon using Camoufox stealth browser.
homepage: https://www.amazon.com
metadata: {"nanobot":{"emoji":"🛒"}}
---

# Amazon Product Scraper

This skill enables scraping product details from Amazon.com using the Camoufox anti-detect browser to bypass bot protection and Cloudflare challenges.

## When to use

Use this skill when you need to:

- Extract product prices, descriptions, reviews, and ratings from Amazon
- Search for products on Amazon and retrieve listings
- Get structured data from Amazon product pages or search results
- Perform multi-step workflows like searching, navigating to products, and extracting details

## Prerequisites

- Camoufox must be installed: `pip install camoufox[geoip]`
- Python environment with nanobot and Camoufox tools loaded
- Basic knowledge of CSS selectors for targeting elements on Amazon pages

## How to use

### 1. Fetch a Product Page

Use the `camoufox_fetch` tool to load an Amazon product page and extract content.

Example for a single product:

```json
{
  "url": "https://www.amazon.com/Lenovo-ThinkPad-Business-Win11Pro-Dockztorm/dp/B0CFWKP1HT/ref=sr_1_5?crid=1D0XLBC8ZINLK&dib=eyJ2IjoiMSJ9.n4KzIlv3gR39_Sw5UWCdpZVZN6A8IvTCB5YmQ719kUhfUCMfpwaDjYzXFLM4H6W8Bhf58pMat1Dy4AyMrYpW67Cz6eV8MECh6atuCqAgQQLC9DmBjYhSDmE2DgNDCf3sD4hlYUxXqn6Z6xuQSCbH6Kg3LkkBGTCdm6ntf2m0LYDoeCNLUE-BXZkV3V8FqBFAhcoQLGSDrtPm-cvUDkcP-uaeQzX-welISDueb8ZQk20.YEeUXFNqJyCVnMVp8aSTwOovrzReQ_6TQ3Pt2Dibllo&dib_tag=se&keywords=thinkpad+e14&qid=1772177203&sprefix=thinkpad+e%2Caps%2C260&sr=8-5",
  "extractMode": "html",
  "waitSeconds": 3,
  "sessionId": "amazon_session"
}
```

This loads the page, waits for JS rendering, and returns the HTML content.

### 2. Extract Product Data

Use the `camoufox_script` tool to run JavaScript on the loaded page and extract structured data.

Example script to get product title, price, and rating:

```javascript
(() => {
  const title = document.querySelector('#productTitle')?.textContent?.trim();
  const price = document.querySelector('.a-price .a-offscreen')?.textContent?.trim();
  const rating = document.querySelector('.a-icon-star .a-icon-alt')?.textContent?.trim();
  const reviews = document.querySelector('#acrCustomerReviewText')?.textContent?.trim();
  return { title, price, rating, reviews };
})()
```

Call with:

```json
{
  "script": "(() => { const title = document.querySelector('#productTitle')?.textContent?.trim(); const price = document.querySelector('.a-price .a-offscreen')?.textContent?.trim(); const rating = document.querySelector('.a-icon-star .a-icon-alt')?.textContent?.trim(); const reviews = document.querySelector('#acrCustomerReviewText')?.textContent?.trim(); return { title, price, rating, reviews }; })()",
  "sessionId": "amazon_session",
  "waitSeconds": 1
}
```

### 3. Search for Products

Navigate to an Amazon search URL and extract search results.

First, use `camoufox_action` or `camoufox_fetch` to load the search page:

```json
{
  "url": "https://www.amazon.com/s?k=laptop",
  "extractMode": "html",
  "waitSeconds": 3,
  "sessionId": "amazon_session"
}
```

Then, use `camoufox_script` to extract product listings:

```javascript
(() => {
  const products = [];
  document.querySelectorAll('.s-result-item').forEach(item => {
    const title = item.querySelector('h2 a span')?.textContent?.trim();
    const price = item.querySelector('.a-price .a-offscreen')?.textContent?.trim();
    const link = item.querySelector('h2 a')?.href;
    if (title && price) {
      products.push({ title, price, link });
    }
  });
  return products.slice(0, 10);  // Top 10 results
})()
```

### 4. Multi-Step Workflows

Use `sessionId` to maintain browser state across calls. For example:

- Start a session and search for products
- Click on a product link using `camoufox_action`
- Extract detailed information

Example action to click on the first search result:

```json
{
  "actions": [
    {
      "action": "click",
      "selector": ".s-result-item h2 a",
      "waitForSelector": "#productTitle"
    }
  ],
  "sessionId": "amazon_session",
  "extractAfter": true,
  "extractMode": "markdown"
}
```

### 5. Handle Anti-Bot Measures

- Always use `waitSeconds` to allow page loading
- Use `sessionId` for persistent sessions to mimic human behavior
- If blocked, try different proxies or geoip settings in Camoufox

## Notes

- Amazon frequently changes its page structure; update selectors as needed
- Respect Amazon's terms of service, robots.txt, and rate limits
- For large-scale scraping, consider using proxies and rotating user agents
- Camoufox sessions auto-expire after 5 minutes of inactivity
- Use `camoufox_session` tool to manage sessions if needed

## Examples

### Scrape a specific product

1. Fetch page with `camoufox_fetch`
2. Extract data with `camoufox_script`

### Search and scrape multiple products

1. Fetch search page
2. Extract listings with script
3. Loop through product URLs with actions or new fetches

This skill leverages the full power of Camoufox for reliable Amazon data extraction.