# API and webhooks

## Getting an API token
The REST API is available on the Team plan and above. Create a personal access token in Profile > Developer > Tokens. Tokens expire after 1 year by default, and you can choose a shorter expiry when you create one. The API base URL is https://api.orbitask.example/v2.

## Rate limits
Each token can make 100 requests per minute. On Business and Enterprise the limit is 300 requests per minute. If you go over the limit, the API returns HTTP 429 with a Retry-After header that tells you how many seconds to wait.

## Webhooks
Admins can register webhooks in Settings > Developer > Webhooks for these events: task.created, task.updated, task.completed and comment.created. Every webhook request is signed with HMAC-SHA256, and the signature is sent in the X-Orbitask-Signature header so you can verify it came from us.

## Webhook retries
If your endpoint does not answer with a 2xx status within 10 seconds, we retry up to 5 times with exponential backoff over about one hour. After the last failed retry, the event is dropped and the webhook is marked as failing in the settings page.
