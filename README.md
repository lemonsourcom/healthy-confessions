# Healthy Confessions

## Railway

1. Deploy this repository as one web service using the included Dockerfile.
2. Add a persistent volume to that service at `/data`. Run one replica. The volume holds SQLite, the queue and the receipt counter.
3. Set `PRINT_TOKEN` to a random secret of at least 32 characters in the service variables. To generate one locally, run `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Never commit the token.
4. Generate a public HTTPS domain. Use its URL for the QR code once the complete flow is tested.

## Print station

The included agent uses the Epson TM-T20IV on the local network over TCP port 9100. Put the laptop and Epson on the same network. Run `python3 print_agent.py` on Mac or `py print_agent.py` on Windows. Enter the Railway URL, the printer's LAN IP and the same PRINT_TOKEN. Keep the laptop awake. USB requires a separate printer connection implementation.

Test with a short confession and one at the 300-character limit. Compare the number on the phone and paper, and check the cutter. The job status “sent” means the bytes went to the printer socket. Check actual paper output at the event.

If the laptop drops out mid-send, the queue does not automatically resend a claimed or uncertain job. Confirm whether the bon printed before returning such a job to the queue. With the agent stopped, an operator can run this SQL on the SQLite volume for the specific job: `UPDATE jobs SET state='queued' WHERE id=42 AND state IN ('claimed','uncertain') AND payload IS NOT NULL;` Replace 42 with the receipt number. Keep the volume to preserve the numbering. Use a separate volume for tests if the event needs to start at 001.
