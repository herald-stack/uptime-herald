# Building Locally

The two compose files in this repository, `docker-compose-mariadb.yaml` and
`docker-compose-postgres.yaml`, build the image from the Dockerfile in this folder. They are
meant for development and testing. To run one from a clone:

1. Copy the matching example file to `.env`, such as `mariadb.env.example`, and fill in the
   values that are left blank.
2. Download the default configuration files from
   [uptime-starter-files](https://github.com/herald-stack/uptime-starter-files), from
   `releases/latest/config/`, into a `config` folder next to the compose file. Rename each
   `.default.yaml` file to `.yaml`, so `config.default.yaml` becomes `config.yaml`.
3. Run `docker compose -f docker-compose-mariadb.yaml up --build`.
