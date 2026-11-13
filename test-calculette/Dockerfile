ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

COPY www /app/www
COPY run.sh /run.sh
RUN chmod a+x /run.sh \
    && sed -i 's/\r$//' /run.sh

CMD ["/run.sh"]
