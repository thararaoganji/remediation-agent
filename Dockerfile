# Runs techdebt_agent (run_local.py) to completion in one container.
# Needs java + mvn + gradle on PATH, not just Python -- SetupStep's preflight
# check and the adapters' compile/build/test calls shell out to whichever of
# these the checked-out target project actually uses (adapters/base.py's
# _mvn_cmd / _gradle_cmd prefer the target repo's own wrapper script when
# present, but the wrapper still needs a JDK on the PATH to run against).
FROM eclipse-temurin:21-jdk-jammy

# Bump if a target project needs a newer Gradle than its own wrapper can
# resolve on its own (rare - the wrapper normally downloads its own).
ENV GRADLE_VERSION=8.10.2

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      python3 \
      python3-pip \
      python3-venv \
      maven \
      unzip \
      curl \
      ca-certificates \
    && curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && ln -s "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle \
    && rm /tmp/gradle.zip \
    && rm -rf /var/lib/apt/lists/*

# Nothing in git_tools.py sets GIT_AUTHOR_NAME/EMAIL on the git subprocess
# (confirmed by inspection -- the .env keys of the same name are read by
# nothing) -- git commit fails outright ("Please tell me who you are")
# without a configured identity, so it's set here at the system level
# instead. `safe.directory '*'` is needed because the target repo gets
# cloned/mounted by a different UID than the one git expects by default in
# newer git versions, which otherwise refuses to operate on it at all.
RUN git config --system user.name "gemini-agent" \
    && git config --system user.email "gemini-agent@local" \
    && git config --system --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

# Every run needs its own scratch space; Cloud Run Jobs give each execution
# a fresh container filesystem, so this is safe to keep ephemeral rather
# than a mounted volume.
ENV WORKSPACE_ROOT=/tmp/sonar_autofix_workspaces
RUN mkdir -p "$WORKSPACE_ROOT"

ENTRYPOINT ["python3", "run_local.py"]
