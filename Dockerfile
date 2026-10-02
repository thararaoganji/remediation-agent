# Runs one of the three agents (run_local.py, AGENT_TYPE=techdebt|coverage|
# duplicate, default techdebt) to completion in one container -- same image
# for all three, only the env var differs between deployments.
# Needs java + mvn + gradle on PATH, not just Python -- SetupStep's preflight
# check and the adapters' compile/build/test calls shell out to whichever of
# these the checked-out target project actually uses (adapters/base.py's
# _mvn_cmd / _gradle_cmd prefer the target repo's own wrapper script when
# present, but the wrapper still needs a JDK on the PATH to run against).
#
# Only JDK 21 (the base image's own) ships in this image -- deliberately
# NOT every JDK version a target project might declare. Cloud Run Jobs
# pull this image fresh on every execution, so bundling e.g. 8/11/17/21/25
# would mean every single run pays that size cost, for versions most runs
# never touch. Instead, core/adapters/jdk_provisioning.py downloads
# whichever exact version a target project's pom.xml/build.gradle declares
# from Adoptium's own API, once per version per container, the first time
# it's actually needed -- see that module's docstring. Requires outbound
# internet access to api.adoptium.net for any version other than 21; keep
# that in mind if this Job's egress is ever locked down to a VPC (see
# docs/GCP_DEPLOYMENT.md's networking section).
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
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY . .

# Every run needs its own scratch space; Cloud Run Jobs give each execution
# a fresh container filesystem, so this is safe to keep ephemeral rather
# than a mounted volume. The agent derives its actual clone dir from this
# base, per agent (/tmp/sonar_remediation_<slug>/) -- see
# git_tools.agent_workspace_root; only the parent (/tmp) matters here, and
# resolve_source() creates the dir itself, so no mkdir needed.
ENV WORKSPACE_ROOT=/tmp/sonar_remediation_workspaces

# The base image bakes in LANGUAGE=en_US:en as a glibc locale var, which
# collides with run_local.py's own LANGUAGE (java/java-maven/java-gradle --
# which build adapter to use) since both are just plain env vars with the
# same name. REQUIRED's missing-key check only verifies it's non-empty, so
# without this override a run that forgets to pass LANGUAGE explicitly
# silently gets "en_US:en" as its language instead of failing loudly --
# confirmed the hard way while testing core/tools/local_secrets.py. "java"
# is the only language this app supports today anyway (see README.md's
# Setup table), so it's a correct default, not just a quieter failure mode
# -- `docker run -e LANGUAGE=...`/compose's `-e` still overrides this, same
# as any other image-baked ENV default.
ENV LANGUAGE=java

ENTRYPOINT ["python3", "run_local.py"]
