# OpenAI worker configuration

Use agents/openai_worker.py as a provider adapter for the platform-neutral worker
protocol. Set OPENAI_API_KEY and READING_MODEL in the launching process's
environment. The model must be available to that API account and support
Responses JSON output. Credentials are not read from the desktop chat session
and are not saved in project profiles.

Example worker_command:
`["/absolute/python", "/absolute/skill/agents/openai_worker.py"]`.

Each worker process makes one request to https://api.openai.com/v1/responses,
with store:false, no previous_response_id and no tools. Input is only the bounded
Context Builder packet plus the fixed generation instructions. The adapter
returns complete draft JSON and provider token usage. max_output_tokens covers
the provider's entire output allowance, including reasoning tokens. An incomplete
response caused by max_output_tokens becomes overflow, which the scheduler
handles by local replanning.

References: [text generation](https://developers.openai.com/api/docs/guides/text),
[token counting](https://developers.openai.com/api/docs/guides/token-counting).

This release tests the HTTP transport with mocks. It does not claim a live paid
model call, account eligibility, or translation-quality validation.
