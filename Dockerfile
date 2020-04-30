FROM python:3.8-alpine
LABEL maintainer="thimic@gmail.com"
ARG VCS_REF
ARG BUILD_DATE
ARG buildno
ARG USER=ncsync
ARG USER_UID=1000
ARG USER_GID=1000

ENV USER=$USER \
    USER_UID=$USER_UID \
    USER_GID=$USER_GID \
    NEXTCLOUDURL="" \
    SOURCEDIR="/media/nextcloud/"


# create group and user
RUN addgroup -g $USER_GID $USER && adduser -G $USER -D -u $USER_UID $USER

# update repositories and install nextcloud-client
RUN apk update && apk add nextcloud-client && rm -rf /etc/apk/cache

# install fswatch
RUN apk add --no-cache \
    file \
    git \
    autoconf \
    automake \
    libtool \
    gettext \
    gettext-dev \
    make \
    g++ \
    texinfo \
    curl

ENV ROOT_HOME /root
ENV FSWATCH_BRANCH master

WORKDIR ${ROOT_HOME}
RUN git clone https://github.com/emcrisostomo/fswatch.git

WORKDIR ${ROOT_HOME}/fswatch
RUN git checkout ${FSWATCH_BRANCH}
RUN ./autogen.sh && ./configure && make && make install && rm -rf ../fswatch

# add run script
WORKDIR /usr/src/nextcloud_filewatch

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY nextcloud_filewatch ./nextcloud_filewatch

ENV PYTHONPATH "${PYTONPATH}:/user/src/nextcloud_filewatch"

CMD [ "python", "./nextcloud_filewatch/main.py" ]
