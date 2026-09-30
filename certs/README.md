# TLS certificate

Put the site's certificate here as two files:

| File            | Contents                                   |
|-----------------|--------------------------------------------|
| `fullchain.pem` | the certificate, followed by any intermediates |
| `privkey.pem`   | its private key                            |

The `web` container checks for them each time it starts:

- **Both present:** it serves HTTPS on port 443 and redirects HTTP to it.
- **Missing:** it serves plain HTTP on port 80.

After adding or renewing the certificate, run `docker compose restart web`.

The `.pem` files are ignored by git. Never commit a private key.

See the main README ("HTTPS") for how to get a certificate.
