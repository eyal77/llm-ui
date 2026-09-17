# Build "LLM Compare": a web app for comparing LLM answers

Build a web app called **LLM Compare**. It sends the same prompt to several LLM providers and compares their answers, token usage and latency. It has three pages: **Single model**, **Compare all** and **Admin**. The spec below is complete; follow it closely.

---

## 1. Architecture

- **Frontend:** React pages. Keep the layout clean and professional, with light and dark mode support, and make it work on mobile.
- **Backend:** Base44 backend functions (TypeScript on Deno). **All calls to LLM providers happen in backend functions, never in the browser.** API keys must never be sent to the frontend.
- **Auth:** use Base44's built-in auth.
  - Every logged-in user can use Single model and Compare all.
  - Only users with the **admin** role can open the Admin page or call the admin functions. Check `user.role === "admin"` inside each admin function, not only in the UI.
- **Credential storage:** the entity described in section 2. Admins edit it in the Admin page. Backend functions read it with `asServiceRole`.

## 2. Data model

### Entity `ProviderConfig`

There is one record per provider. Restrict read and write to admins only (row-level security). Backend functions access it via `asServiceRole`.

| Field | Type | Notes |
|---|---|---|
| `provider` | string, unique | One of `bedrock`, `nvidia`, `gemini`, `openai`, `anthropic` |
| `api_key` | string | NVIDIA, Gemini, OpenAI and Anthropic keys |
| `aws_access_key_id` | string | Bedrock only |
| `aws_secret_access_key` | string | Bedrock only |
| `aws_session_token` | string | Bedrock only, optional |
| `aws_region` | string | Bedrock only. Default `us-east-1` |
| `base_url` | string | NVIDIA (default `https://integrate.api.nvidia.com/v1`) and OpenAI (default `https://api.openai.com/v1`). Optional |
| `workspace_id` | string | Anthropic only, optional. It must match `^wrkspc_[A-Za-z0-9]+$` |
| `models` | string[] | Empty means use the default models below |

### Default models

These are used when `models` is empty:

| Provider | Default models |
|---|---|
| bedrock | `us.amazon.nova-2-lite-v1:0` |
| nvidia | `nvidia/nemotron-3-super-120b-a12b`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` |
| gemini | `gemini-3.5-flash-lite` |
| openai | `gpt-5.6-luna` |
| anthropic | `claude-haiku-4-5` |

### When a provider counts as configured

A provider is **configured** only when its required credentials are set:
- Bedrock needs the access key ID and the secret access key.
- Every other provider needs `api_key`.

Unconfigured providers are listed as "skipped (no credentials)" and are never called.

## 3. Backend functions

Every function returns JSON.

### `listModels` (any logged-in user)

Returns an entry for each of the 5 providers:

```
{ provider, label, configured, supports: {temperature, top_p, top_k}, notes,
  models: [{ id: "<provider>:<model>", model }] }
```

- `models` is an empty array when the provider isn't configured.
- The function also returns the parameter defaults (see section 4).

Labels:

| Provider | Label |
|---|---|
| bedrock | AWS Bedrock |
| nvidia | NVIDIA |
| gemini | Google Gemini |
| openai | OpenAI |
| anthropic | Anthropic |

`supports` is true for all three knobs, except that OpenAI has `top_k: false`.

### `generate` (any logged-in user)

**Input:**

```
{ provider, model, system, user, max_tokens, temperature, top_p, top_k,
  use_temperature, use_top_p, use_top_k, reasoning }
```

**Validation:**
- `user` must not be empty.
- `max_tokens` is between 1 and 200000.
- `temperature` is between 0 and 2.
- `top_p` is between 0 and 1.
- `top_k` is between 1 and 1000.
- `model` must be one of the provider's configured models.

**Response:**
- It always returns HTTP 200, because a failing provider is a result to show, not a broken request.
- On success:
  ```
  { ok: true, provider, model, text, input_tokens, output_tokens, total_tokens,
    reasoning_tokens (number or null), truncated, stop_reason, latency_ms,
    sent: {param: value}, skipped: ["top_k: not supported by OpenAI", ...], warning }
  ```
- On failure:
  ```
  { ok: false, provider, model, latency_ms, error: "<ErrorType>: <message>" }
  ```
  Truncate the error message to 1500 characters. Never include any key in it.

**Timing and warnings:**
- `latency_ms` measures only the provider call.
- If `text` is empty, set `warning` to "No answer text — the model likely spent max_tokens on reasoning."
- If `truncated` is true, set `warning` to "Cut off at max_tokens — the answer is incomplete."

**Sampling rules**, which apply to all providers:
- `sent` always contains `max_tokens` and `reasoning`.
- For each knob (temperature, top_p, top_k) whose `use_*` flag is true:
  - If `reasoning` is true, don't send the knob. Add `"<knob>: reasoning is on (the model samples for itself)"` to `skipped`.
  - Otherwise, if the provider doesn't support the knob, don't send it. Add `"<knob>: not supported by <Label>"` to `skipped`.
  - Otherwise, send the knob and record it in `sent`.

### `adminGetConfig` (admin only)

Returns every provider with these fields:
- **Labels:** friendly labels. Never show environment-variable names in the UI.
- **Secret fields:** a masked value only, such as `nvap••••ijkl` (first 4 characters + `••••` + last 4), or `••••` when the value is 8 characters or fewer. Also return `is_set`.
- **Models:**
  - `models` holds the stored list.
  - `default_models` holds the defaults.
  - `is_default` is true when the stored list is empty.
- **Configured flag:** whether the provider is configured.

### `adminSaveConfig` (admin only)

**Input:**

```
{ provider, values: {field: value}, clear: [field] }
```

**Rules:**
- Trim all values.
- **Secret fields:** a blank value means "keep the current value". To remove a secret, the field must be listed in `clear`.
- **Models:** accept text with one model ID per line or comma-separated. Normalize it by trimming, dropping empty entries and removing duplicates. An empty list, or one identical to the defaults, is saved as empty, meaning "use the defaults".
- **Workspace ID:** reject any value that doesn't match `^wrkspc_[A-Za-z0-9]+$` with the message: "Workspace ID must be a workspace's ID, like wrkspc_01Jw… (not the key's scope). Copy it from Claude Console → Settings → Workspaces, ID column."
- **Unknown fields:** reject them with a 400.

Return the same shape as `adminGetConfig`.

### `adminTestProvider` (admin only)

**Input:** `{ provider }`

**Behavior:**
- Call `generate` logic with these settings:
  - model: the provider's first model
  - user: "Reply with the single word: OK"
  - max_tokens: 300
  - all sampling knobs off
- Return `{ ok, model, text (first 200 characters), latency_ms }` or `{ ok: false, model, error }`.

## 4. Parameters and defaults

| Parameter | Default | Sent by default |
|---|---|---|
| temperature | 0.3 | yes |
| top_p | 0.9 | no |
| top_k | 40 | no |
| max_tokens | 2000 | always |
| reasoning | off | n/a |

top_p and top_k start switched off because several current models reject them, or reject temperature and top_p together.

## 5. Provider calls

Make these REST calls from the backend functions.

**Common rules:**
- Use `fetch`.
- Use a 60-second timeout for NVIDIA and 120 seconds for the other providers.
- If a provider returns HTTP 4xx or 5xx, throw an error that includes the status code and the response body.
- Answer text must never include reasoning or thinking content.

### OpenAI

- **Request:** `POST {base_url}/chat/completions` with header `Authorization: Bearer <api_key>`.
- **Body:**
  - `model`
  - `messages`: a system message (only if `system` isn't empty), then the user message.
  - `max_completion_tokens`
  - `reasoning_effort`: `"low"` if reasoning is on, otherwise `"none"`.
  - `temperature` and `top_p` if enabled.
- **Retry:** if the response is a 400 whose body mentions `reasoning_effort`, send the request again without `reasoning_effort`. Add `"reasoning: model does not accept reasoning_effort (retried without it)"` to `skipped`.
- **Result:**
  - `text` = `choices[0].message.content`
  - `input_tokens` = `usage.prompt_tokens`
  - `output_tokens` = `usage.completion_tokens`
  - `reasoning_tokens` = `usage.completion_tokens_details.reasoning_tokens`
  - `truncated` is true when `finish_reason === "length"`

### NVIDIA (OpenAI-compatible)

- **Request:** `POST {base_url}/chat/completions`, Bearer auth.
- **Body:**
  - `model`
  - `messages`, same as OpenAI.
  - `max_tokens`
  - `chat_template_kwargs: { enable_thinking: <reasoning> }`
  - `temperature` and `top_p` if enabled.
  - `top_k` as a top-level integer if enabled.
- **Result:** parse it the same way as OpenAI.
- **Special case:** if `finish_reason === "length"` and `message.reasoning_content` equals `message.content`, the model ran out of tokens while thinking. Treat the text as empty.

### Google Gemini

- **Request:** `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with header `x-goog-api-key`.
- **Body:**
  - `contents: [{ role: "user", parts: [{ text }] }]`
  - `systemInstruction: { parts: [{ text: system }] }`, only if `system` isn't empty.
  - `generationConfig`, containing:
    - `maxOutputTokens`
    - `temperature`, `topP` and `topK` if enabled.
    - `thinkingConfig: { thinkingLevel: "low" }` only when reasoning is on.
- **Result:**
  - `text`: join the `text` of `candidates[0].content.parts`, skipping parts with `thought: true`.
  - `input_tokens` = `usageMetadata.promptTokenCount`
  - `output_tokens` = `candidatesTokenCount` + `thoughtsTokenCount`. Adding the thinking tokens makes the numbers comparable with the other providers.
  - `reasoning_tokens` = `thoughtsTokenCount`
  - `truncated` is true when `finishReason === "MAX_TOKENS"`

### Anthropic

- **Request:** `POST https://api.anthropic.com/v1/messages`.
- **Headers:**
  - `x-api-key`
  - `anthropic-version: 2023-06-01`
  - `content-type: application/json`
  - `anthropic-workspace-id: <workspace_id>`, only when `workspace_id` is set. Keys whose scope is **Organization** need it; keys scoped to a single workspace don't.
- **Body:**
  - `model`, `max_tokens`
  - `system`, if not empty
  - `messages`
  - `thinking: { type: "disabled" }` when reasoning is off.
  - `temperature`, `top_p` and `top_k` only if enabled.
- **Result:**
  - `text`: join the `text` of the content blocks with `type === "text"`.
  - `input_tokens` = `usage.input_tokens`
  - `output_tokens` = `usage.output_tokens`
  - `truncated` is true when `stop_reason === "max_tokens"`
- **Error hints:**
  - If the error mentions `anthropic-workspace-id` and no workspace ID is set, append: " — This key needs a workspace: set 'Workspace ID' in Admin → Anthropic."
  - If it says "valid workspace ID" or "Workspace `…` not found", append: " — Check 'Workspace ID' in Admin → Anthropic: it must be a workspace's ID (wrkspc_…) that this key can access."

### AWS Bedrock (Converse API)

- **Request:** `POST https://bedrock-runtime.{region}.amazonaws.com/model/{encodeURIComponent(model)}/converse`.
- **Signing:** sign it with AWS SigV4 (service `bedrock`). Use `npm:aws4fetch` (`AwsClient` with accessKeyId, secretAccessKey, sessionToken, region, service `"bedrock"`) or `npm:@aws-sdk/client-bedrock-runtime` (`ConverseCommand`).
- **Body:**
  - `messages: [{ role: "user", content: [{ text }] }]`
  - `system: [{ text }]`, if not empty.
  - `inferenceConfig: { maxTokens, temperature?, topP? }`
  - `additionalModelRequestFields`, only when non-empty:
    - **top_k:**
      - If the model ID contains `nova`, send `{ inferenceConfig: { topK } }`.
      - If it contains `anthropic`, send `{ top_k }`.
      - Otherwise don't send it. Add `"top_k: no known top_k field for this Bedrock model family"` to `skipped`.
    - **Reasoning on:**
      - For `nova` models, add `reasoningConfig: { type: "enabled", maxReasoningEffort: "low" }`.
      - For other models, add `"reasoning: switch implemented for Amazon Nova models only"` to `skipped`.
- **Result:**
  - `text`: join the `text` of `output.message.content` blocks, ignoring `reasoningContent` blocks.
  - `input_tokens` = `usage.inputTokens`
  - `output_tokens` = `usage.outputTokens`
  - `truncated` is true when `stopReason === "max_tokens"`

## 6. Pages and UI

### Top bar

- The app name "LLM Compare".
- Tabs: **Single model** | **Compare all** | **Admin**. The Admin tab is visible only to admins.

### Shared request panel

The panel sits on the left and is sticky on desktop. It's shared by Single model and Compare all, so the text and settings stay when the user switches tabs. It also keeps drafts across page reloads.

- **Model selection:**
  - **Single model:** a dropdown of models grouped by provider label. Unconfigured providers appear as a disabled "Set credentials in Admin" entry. Below the dropdown, show the provider's note.
  - **Compare all:** a checklist of every configured model (provider pill + model ID), all checked by default, with "All · None" links and an "(N selected)" count. Below it, show "Skipped (no credentials): …".
- **Messages:** a System message textarea and a User message textarea (required).
- **Parameters:** temperature, top_p and top_k each have a checkbox, a slider and a number input. max_tokens has a number input only and is always sent. Add a "Reasoning on (drops temperature / top_p / top_k)" checkbox and a "Reset to defaults" link.
  - A disabled knob's inputs are greyed out. Knobs are also greyed out while reasoning is on.
- **Warnings:** a live warning line, e.g. "top_k is not sent to OpenAI.", based on the selected models.
- **Run button:** it reads "Run" or "Run on selected models". Ctrl+Enter also runs.

If no provider is configured, show a notice with a link to Admin.

### Single model result

A result card with:
- A header with the provider pill (a distinct color per provider), the model ID and a status chip. The chip is one of:
  - `OK`
  - `TRUNCATED`
  - `EMPTY`
  - `ERROR`
- **Four metric tiles:**
  - Input tokens
  - Output tokens, with "incl. N reasoning" when known
  - Total tokens
  - Latency (e.g. "850 ms" or "2.5 s"). Latency shows only the time; don't show the stop reason.
- **Warning box:** only when there is a warning.
- **Response:**
  - A "Response" heading with a word count and a character count, and a **Copy** button.
  - The answer text in a scrollable box that keeps line breaks.
- A collapsible "Parameters sent · N skipped" section that lists the sent values and the skipped reasons.
- **On failure:** a red error box with the error text and the latency.

### Compare all

**Running:**
- Call `generate` once per selected model **in parallel** from the frontend.
- The table and cards fill in as each call returns. Rows that are still pending show a spinner.
- When everything is done, show a toast: "All models answered" or "Done — X of N failed". One failing model never stops the others.

**Comparison table** ("Comparison · k/N done"), with columns:
- Model: provider pill + model ID
- Status
- Latency, with a small bar relative to the slowest model
- Input tokens
- Output tokens, with a bar and a reasoning count when known
- Total tokens
- Words
- Similarity to the baseline, as a percentage with a green bar. The baseline row shows "baseline".

**Table markers and behavior:**
- ⚡ marks the fastest model and ↓ marks the model with the fewest total tokens. Only show these when at least 2 models succeeded.
- Clicking a row scrolls to that model's card.

**Toolbar:**
- A **Baseline** dropdown. The default baseline is the first selected model.
- A segmented control: **Side by side** | **Diff vs baseline**.
- In diff mode, a legend: ~~only in baseline~~ (red) and "only in this answer" (green).

**Cards:**
- A responsive grid with one card per model.
- Each card shows the header, "in · out · latency", any warning, the answer text or the diff, the parameters section and a Copy button.
- The baseline card is highlighted.

**Export:**
- **Export CSV** and **Export JSON**. Enable them only when all calls are done.
- CSV columns:
  - provider, model, ok
  - latency_ms, input_tokens, output_tokens, reasoning_tokens, total_tokens
  - truncated, stop_reason
  - similarity_to_baseline, response, error
- The CSV is UTF-8 with a BOM. Every cell is quoted, with inner quotes doubled.
- JSON contains `{ request, baseline, results }`.
- File name: `llm-compare-<timestamp>`.

### Word-level diff (implement it in the frontend, with no library needed)

**Tokens:**
- Split each text into word tokens with `/\S+\s*/g`. Each token is a word plus its trailing whitespace.
- Compare tokens by their trimmed value.

**Algorithm:**
1. Trim the common prefix and suffix.
2. Run an LCS dynamic-programming pass over the middle, stored in a `Uint16Array`.
3. If `(n+1)*(m+1)` is over 6,000,000, fall back to the same algorithm on lines.
4. If lines are still too large, show "Too long to diff".

**Output:**
- Produce `eq`, `del` and `ins` operations and merge consecutive operations of the same type.
- Render `del` as a red strikethrough and `ins` as green. Keep trailing whitespace outside the highlight.
- **Similarity** = `2 × equal tokens / (tokens A + tokens B)`.
- Escape all HTML.

### Admin page (admins only)

**Header:** the title "Admin — provider credentials", with the hint "A provider is used only when its credentials are set."

**Provider cards** (one per provider):
- The provider pill and a chip that reads **configured** or **not configured**.
- Friendly field labels only: Access key ID, Secret access key, Session token (optional), Region (optional), API key, Base URL (optional), Workspace ID (optional). Never show variable names.

**Secret inputs:**
- A password field whose placeholder is "<masked> (saved — blank keeps it)", or "Not set".
- A 👁 show/hide button.
- A "clear" checkbox when a value is already saved.

**Models:**
- A monospace textarea with one model ID per line. It is prefilled with the saved list, or with the defaults plus a "DEFAULTS" chip.
- A "Restore defaults" link.
- The hint: "One model ID per line. Leave empty to use the defaults."

**Anthropic Workspace ID:**
- Placeholder `wrkspc_…`.
- Help text: "Only for API keys whose scope is Organization (not one workspace). Enter the ID of the workspace to use — it starts with wrkspc_ — from Claude Console → Settings → Workspaces, ID column."

**Provider notes:**

| Provider | Note |
|---|---|
| Bedrock | "top_k is model-specific on Bedrock: sent for Amazon Nova and Anthropic models only." |
| NVIDIA | "top_k is passed through extra_body; some hosted models ignore it." |
| OpenAI | "The OpenAI API has no top_k." |
| Anthropic | "Current Claude models reject a non-default temperature, and temperature + top_p together; enable only what your model accepts." |

**Buttons:**
- **Save** sends only the changed fields and shows a toast.
- **Test connection** is enabled only when the provider is configured. It shows "OK — <model> answered in 1.2 s: "OK"", or the error.

## 7. Acceptance checklist

- [ ] A regular user can't open Admin or call the admin functions (server-side check).
- [ ] API keys never appear in any response to the browser, not even in errors.
- [ ] A provider without credentials is skipped and listed as skipped.
- [ ] Compare all runs the models in parallel, and results appear one by one.
- [ ] A failing provider shows its error while the others still show answers.
- [ ] Turning reasoning on removes temperature, top_p and top_k from every request, and lists them as skipped.
- [ ] OpenAI never receives top_k.
- [ ] The diff view highlights word differences against the chosen baseline, and the similarity percentages update when the baseline changes.
- [ ] Emptying a Models box brings back the default models.
- [ ] Saving "Organization" as the Anthropic Workspace ID is rejected with a clear message.
- [ ] The CSV opens correctly in Excel.
