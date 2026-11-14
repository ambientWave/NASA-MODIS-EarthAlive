# syntax=docker/dockerfile:1

# Python 3.14 comes from the official image. GDAL comes from conda-forge as
# libraries + CLI only (libgdal-core/libgdal-hdf4 have no Python dependency),
# so conda can never substitute a different interpreter version.
FROM python:3.14.10-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libexpat1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# `libgdal-core` provides gdal_translate/gdalinfo; `libgdal-hdf4` adds the
# HDF4/HDF4-EOS driver required by MOD11A1 granules.
ARG MICROMAMBA_URL=https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64
RUN curl -fsSL "$MICROMAMBA_URL" -o /usr/local/bin/micromamba \
    && chmod +x /usr/local/bin/micromamba \
    && /usr/local/bin/micromamba create -y -p /opt/gdal -c conda-forge \
        --no-pyc \
        libgdal-core \
        libgdal-hdf4 \
    && /usr/local/bin/micromamba clean --all --yes \
    && rm -f /usr/local/bin/micromamba \
    && python --version > /tmp/python-version.txt \
    && grep -q "3.14" /tmp/python-version.txt \
    && /opt/gdal/bin/gdal_translate --version \
    && /opt/gdal/bin/gdalinfo --formats > /tmp/gdal-formats.txt \
    && grep -qi hdf4 /tmp/gdal-formats.txt \
    && rm -f /tmp/python-version.txt /tmp/gdal-formats.txt

ENV PATH=/opt/gdal/bin:$PATH \
    GDAL_TRANSLATE=/opt/gdal/bin/gdal_translate

# Mirror the local repository layout (backend/backend under backend/), so the
# project-rooted paths in app/services/modis_processor.py resolve to
# /srv/backend/data instead of "/".
WORKDIR /srv/backend/backend

COPY requirements.txt ./
# The import check fails the build immediately if the queue/raster stack did
# not install, instead of surfacing as "exec rq failed" at container start.
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -c "import rq, redis, h5netcdf, h5py, rasterio; print('analysis deps OK')"

COPY app ./app
# Flutter web build served at /home and / (see app/main.py).
COPY flutterweb ./flutterweb
COPY pytest.ini ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]
