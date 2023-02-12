FROM nvidia/cuda:11.7.1-cudnn8-devel-ubuntu22.04

WORKDIR /app
# Set en_US.UTF-8 locale by default
RUN echo "LC_ALL=en_US.UTF-8" >> /etc/environment

# Install packages
RUN apt-get update && apt-get install -y --no-install-recommends \
  build-essential \
  wget \
  git \
  && apt-get clean autoclean && rm -rf /var/lib/apt/lists/{apt,dpkg,cache,log} /tmp/* /var/tmp/*

RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O install_miniconda.sh && \
  bash install_miniconda.sh -b -p /opt/conda && rm install_miniconda.sh
ENV PATH="/opt/conda/bin:${PATH}"

RUN conda install python~=3.10 pip && \
    pip install --no-cache-dir "torch>=1.12" && \
    conda clean --all && rm -rf ~/.cache/pip

VOLUME /cache
ENV PETALS_CACHE=/cache

COPY requirements.txt /app/requirements.txt

RUN pip install -r requirements.txt

COPY . /app
# CMD [ "python3", "-m" , "flask", "run", "--host=0.0.0.0", "--port=5000"]
# CMD [ "python3", "-m" , "gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--threads", "100", "--timeout", "1000"]
CMD gunicorn --timeout 1000 --workers 1 --threads 100 --log-level debug --bind 0.0.0.0:5000 app:app
