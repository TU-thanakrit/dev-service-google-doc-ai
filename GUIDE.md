# OCR API user guide

This guide shows you how to use **Postman** to send a PDF to the OCR API and get its text. You do not need to write code.

The service is for **development and testing**. Processing a document may incur a charge. Use a PDF that you are allowed to share and have approval to process.

## Before you start

You need:

- **Postman** installed on your computer.
- A PDF file for testing.
- The **API demo key**. This is a private passcode, separate from the browser demo passcode.

If you are using Danny's laptop, the API demo key is saved in `~/.config/ocr-key-api/passcode`. To display it, open a terminal on that laptop and enter:

```bash
cat ~/.config/ocr-key-api/passcode
```

Copy the displayed value. If you cannot access that laptop, ask the service operator for the key through an approved secure channel. Do not put it in chat, screenshots, tickets, or shared files.

## Step 1: Open Postman

1. Open **Postman**.
2. Click **New**.
3. Choose **HTTP**.
4. A new request opens. The menu to the left of the address box should say **GET**. If it does not, select **GET**.

## Step 2: Check that the key works

1. Paste this address into the address box:

   ```text
   https://dev-ocr-key-api-1022222817897.asia-southeast1.run.app/health
   ```

2. Below the address box, click **Headers**.
3. Add one row:
   - In the **Key** box, enter `X-Demo-Key`.
   - In the **Value** box, paste your API demo key.
4. Make sure the row is enabled, then click **Send**.

If the key works, Postman shows **200 OK**. The response shows:

```json
{"status":"ok"}
```

If you see **401**, check that you copied the API demo key correctly. Do not use the browser demo passcode. This check confirms access; it does not process a PDF.

## Step 3: Prepare a PDF request

1. Click **New**, then choose **HTTP** again.
2. Change the menu to the left of the address box from **GET** to **POST**.
3. Paste this address into the address box:

   ```text
   https://dev-ocr-key-api-1022222817897.asia-southeast1.run.app/v1/ocr/jobs
   ```

4. Click **Headers**.
5. Add the first row:
   - **Key:** `X-Demo-Key`
   - **Value:** paste your API demo key.
6. Add the second row:
   - **Key:** `Idempotency-Key`
   - **Value:** create a new, unique name for this test, such as `danny-test-20261110-001`.

The second value is a **request name**. It helps the service recognize the same request if the connection fails. It is not your API demo key. Write it down, and use a different name for each new document. Use at least 8 characters, with letters, numbers, hyphens, or underscores.

## Step 4: Attach and send the PDF

1. Click the **Body** tab.
2. Select **form-data**.
3. In the first row, enter `file` in the **Key** box.
4. In the same row, change **Text** to **File**. You may need to move your pointer over the row to see the choice.
5. Click **Select Files** and choose your PDF.
6. Check that you have selected the intended file.
7. Click **Send** **once**.

Postman sets the file upload format for you. Do not add a `Content-Type` header yourself.

If the service accepts the PDF, Postman shows **202 Accepted**. Find `job_id` in the response. An example is shown below; your response contains additional details and a different value:

```json
{
  "job_id": "7e8c9d40-0573-4f25-a94d-58bd76d79c22",
  "state": "accepted"
}
```

Copy the **job ID from your own response**. Save it somewhere private. The job ID is how you check the result.

**If the request times out or the connection fails, do not click Send again immediately.** The service may already be processing the PDF. Keep the same PDF and request name, and ask the service operator to help check the outcome. Sending a new request may cause another charge.

## Step 5: Check whether the PDF is ready

1. Click **New**, then choose **HTTP**.
2. Select **GET** in the menu beside the address box.
3. Paste this address into the address box:

   ```text
   https://dev-ocr-key-api-1022222817897.asia-southeast1.run.app/v1/ocr/jobs/YOUR_JOB_ID
   ```

4. Replace `YOUR_JOB_ID` with the job ID you copied in Step 4. Do not include spaces.
5. Click **Headers** and add one row:
   - **Key:** `X-Demo-Key`
   - **Value:** your API demo key.
6. Click **Send**.
7. Find `state` in the response.

What the state means:

- **`accepted`**: the service received the PDF. Wait about 10 seconds, then click **Send** again.
- **`running`**: the service is reading the PDF. Wait about 10 seconds, then click **Send** again.
- **`succeeded`**: the service finished. Go to Step 6.
- **`partially_succeeded`**: the service finished some pages. Go to Step 6 and review the details.
- **`failed`**: the service could not complete the job. Go to Step 6 and review the reason.

If Postman shows a `Retry-After` value, wait at least that many seconds before checking again.

## Step 6: Get the text

1. Keep the same request open in Postman.
2. Add `/result` to the end of the address. It should look like this, with your own job ID:

   ```text
   https://dev-ocr-key-api-1022222817897.asia-southeast1.run.app/v1/ocr/jobs/YOUR_JOB_ID/result
   ```

3. Keep the `X-Demo-Key` row in **Headers**.
4. Click **Send**.
5. If you see **200 OK**, look for `state` and `text` in the response. Check any warnings and page details too.
6. Compare the extracted text with the original PDF. The service can make reading mistakes.

If you see **202**, the job is not ready. Wait for the number of seconds shown in `Retry-After`, then click **Send** again. A **200** result can still have a `failed` or `partially_succeeded` state, so check the state as well as the number.

The text is returned in a JSON response. JSON is a way to organize information using labels such as `text` and `state`.

## Step 7: Save the result

1. In the Postman response area, open **View more actions**.
2. Choose **Save response to file**.
3. Save the file in an approved location on your computer. Use a name such as `ocr-result.json`.
4. Open the file when needed to review or share the result.

The saved file can contain text from your PDF. Protect it according to your organization's data-handling rules.

## If something goes wrong

- **401:** Check the `X-Demo-Key` value. Ask the service operator if you are unsure which key to use.
- **400:** Check that `Idempotency-Key` has at least 8 allowed characters and that you entered `file` as the upload name.
- **409:** That request name is already associated with different file content. Stop and check the original request.
- **413:** The file or request is too large. Ask the operator for the current limits.
- **415 or 422:** Check that you selected a readable PDF and chose **File**, not **Text**. The PDF must not be encrypted or exceed the service's limits.
- **429:** The service may be busy. If a waiting time is shown in `Retry-After`, honor it and ask the operator before trying the upload again.
- **404:** Check that you copied the entire job ID. The job may also no longer be available.
- **410:** The job has expired. Ask the operator about the next step.
- **502, 503, 504, or a timeout during upload:** The outcome may be uncertain. Do not automatically resend the PDF. Keep the PDF and original request name, and ask the operator for help.

If an operator approves retrying the same uncertain upload, use the **same PDF and the same request name**. Do not create a new request name for that retry. This protection is not permanent; after a job expires or is cleaned up, the same name may create a new billable job.

## What has been tested

On 10 November 2026, one approved synthetic one-page PDF was sent through this API. The service accepted it, finished the job, and returned a result containing the expected test reference. This confirms that one test worked; it does not guarantee that every document will be read accurately.

All users of the development API key share the same service access. This service is not intended for production use.
