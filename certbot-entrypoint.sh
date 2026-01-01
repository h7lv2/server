#!/bin/sh
echo "dns_cloudflare_api_token = $CLOUDFLARE_API_TOKEN" > /cloudflare.ini
chmod 600 /cloudflare.ini

for DOMAIN in $(env | grep '^DOMAIN_' | cut -d= -f2); do
    if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
        STAGING_FLAG=$([ "$STAGING" = "true" ] && echo "--staging" || echo "")
        certbot certonly --authenticator dns-cloudflare \
            --dns-cloudflare-credentials /cloudflare.ini \
            --dns-cloudflare-propagation-seconds 60 \
            $STAGING_FLAG -d "$DOMAIN" -d "*.$DOMAIN" \
            --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
    fi
done

trap exit TERM; while :; do certbot renew --non-interactive; sleep 12h & wait ${!}; done;
