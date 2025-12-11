pipeline {
  agent any

  options {
    timestamps()
    ansiColor('xterm')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    durabilityHint('PERFORMANCE_OPTIMIZED')
  }

  parameters {
    string(name: 'PY_VERSION', defaultValue: '3.11', description: 'Python version for the build agent')
    string(name: 'AZ_SUBSCRIPTION', defaultValue: '', description: 'Azure Subscription ID')
    string(name: 'AZ_RESOURCE_GROUP', defaultValue: '', description: 'Azure Resource Group name')
    string(name: 'AZ_FUNCTIONAPP', defaultValue: '', description: 'Function App name (without slot)')
    string(name: 'AZ_SLOT', defaultValue: 'staging', description: 'Deployment slot for pre-prod')
    booleanParam(name: 'RUN_DAST', defaultValue: true, description: 'Run OWASP ZAP baseline scan')
    booleanParam(name: 'ENABLE_PIP_AUDIT', defaultValue: true, description: 'Run pip-audit (SCA)')
  }

  environment {
    // Jenkins Credentials you must create:
    // 1) AZURE_SP_JSON: Secret text credential containing the raw JSON of your Service Principal:
    //    {"clientId":"...","clientSecret":"...","tenantId":"..."}
    // 2) SEMGREP_APP_TOKEN (optional but recommended): Secret text
    AZURE_SP_JSON = credentials('AZURE_SP_JSON')
    SEMGREP_APP_TOKEN = credentials('SEMGREP_APP_TOKEN')
    VENV = '.venv'
    // Staging URL pattern: https://<appname>-<slot>.azurewebsites.net
    STAGING_URL = "https://${AZ_FUNCTIONAPP}-${AZ_SLOT}.azurewebsites.net"
    // Paths for reports
    REPORT_DIR = "build-reports"
  }

  stages {

    stage('Checkout') {
      steps {
        deleteDir()
        checkout scm
        sh 'mkdir -p $REPORT_DIR'
      }
    }

    stage('Set up Python') {
      steps {
        sh """
          python${PY_VERSION} -V || true
          which python${PY_VERSION} || true

          # Prefer pyenv/available python; fallback to 'python3'
          PYBIN=\$(command -v python${PY_VERSION} || command -v python3 || command -v python)
          echo "Using PYBIN=\$PYBIN"
          \$PYBIN -m venv ${VENV}
          . ${VENV}/bin/activate
          python -V
          pip install --upgrade pip wheel
          # Project deps
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          # Tooling
          pip install ruff==0.7.2 black==24.8.0 pytest pytest-cov pytest-html \
                     bandit==1.7.9 pip-audit==2.* junitparser
          # semgrep via pip (works fine in CI)
          pip install semgrep==1.* || true
        """
      }
    }

    stage('Lint') {
      steps {
        sh """
          . ${VENV}/bin/activate
          ruff check .
          black --check .
        """
      }
    }

    stage('Unit Tests') {
      steps {
        sh """
          . ${VENV}/bin/activate
          pytest -q --maxfail=1 --disable-warnings \
                 --junitxml=${REPORT_DIR}/junit.xml \
                 --cov=function_app --cov-report=xml:${REPORT_DIR}/coverage.xml \
                 --cov-report=term-missing \
                 tests || exit 1
        """
      }
      post {
        always {
          junit allowEmptyResults: true, testResults: "${REPORT_DIR}/junit.xml"
          publishHTML(target: [
            reportDir: "${REPORT_DIR}",
            reportFiles: 'pytest-html-report.html',
            reportName: 'PyTest Report',
            keepAll: true,
            allowMissing: true
          ])
          archiveArtifacts artifacts: "${REPORT_DIR}/**", allowEmptyArchive: true
        }
      }
    }

    stage('SAST') {
      steps {
        sh """
          . ${VENV}/bin/activate
          # Bandit
          bandit -q -r . -f xml -o ${REPORT_DIR}/bandit.xml || true
          # Semgrep (community rules)
          semgrep --error --config p/ci --json --output ${REPORT_DIR}/semgrep.json || true
          # Optional SCA
          if [ "${ENABLE_PIP_AUDIT}" = "true" ]; then
            pip-audit -r requirements.txt -f json -o ${REPORT_DIR}/pip-audit.json || true
          fi
        """
      }
      post {
        always {
          archiveArtifacts artifacts: "${REPORT_DIR}/bandit.xml, ${REPORT_DIR}/semgrep.json, ${REPORT_DIR}/pip-audit.json", allowEmptyArchive: true
      }}
    }

    stage('Build: Package ZIP') {
      steps {
        sh """
          mkdir -p dist
          ZIP=dist/functionapp.zip
          rm -f \$ZIP
          # Create clean temp dir to avoid packing junk
          rsync -a --exclude '.git' --exclude '.venv' --exclude 'dist' --exclude '${REPORT_DIR}' \
                 --exclude 'local.settings.json' --exclude 'tests' --exclude 'Jenkinsfile' \
                 ./ dist/stage/
          (cd dist/stage && zip -r ../functionapp.zip .)
          echo "Built artifact: \$ZIP"
        """
      }
      post {
        success {
          archiveArtifacts artifacts: 'dist/functionapp.zip', fingerprint: true
        }
      }
    }

    stage('Azure Login') {
      steps {
        sh """
          # Expect az CLI available on agent. If not, install or use an agent image with az preinstalled.
          echo '${AZURE_SP_JSON}' > sp.json
          CLIENT_ID=\$(jq -r .clientId sp.json)
          CLIENT_SECRET=\$(jq -r .clientSecret sp.json)
          TENANT_ID=\$(jq -r .tenantId sp.json)

          az version || true
          az config set extension.use_dynamic_install=yes_without_prompt
          az login --service-principal -u \$CLIENT_ID -p \$CLIENT_SECRET --tenant \$TENANT_ID
          az account set --subscription "${AZ_SUBSCRIPTION}"
          az account show
        """
      }
    }

    stage('Deploy to Staging Slot') {
      steps {
        sh """
          # Ensure slot exists
          if ! az functionapp deployment slot list -g "${AZ_RESOURCE_GROUP}" -n "${AZ_FUNCTIONAPP}" | jq -e '.[] | select(.name==\"${AZ_SLOT}\")' >/dev/null; then
            echo "Creating slot ${AZ_SLOT}..."
            az functionapp deployment slot create -g "${AZ_RESOURCE_GROUP}" -n "${AZ_FUNCTIONAPP}" --slot "${AZ_SLOT}"
          fi

          # Zip deploy
          az functionapp deployment source config-zip \
              --resource-group "${AZ_RESOURCE_GROUP}" \
              --name "${AZ_FUNCTIONAPP}" \
              --slot "${AZ_SLOT}" \
              --src dist/functionapp.zip

          # Warmup ping (retry a few times)
          for i in 1 2 3 4 5; do
            curl -sSf "${STAGING_URL}/api/health" && break || sleep 5
          done || true
        """
      }
    }

    stage('DAST: ZAP Baseline') {
      when { expression { return params.RUN_DAST } }
      steps {
        sh """
          # Requires Docker on the Jenkins agent
          mkdir -p ${REPORT_DIR}
          # Use provided zap-baseline.conf at repo root
          TARGET="${STAGING_URL}"
          echo "Scanning target: \$TARGET"

          docker run --rm --network host \
            -v "\$PWD/zap-baseline.conf:/zap/wrk/zap-baseline.conf:ro" \
            -v "\$PWD/${REPORT_DIR}:/zap/wrk" \
            owasp/zap2docker-stable zap-baseline.py \
              -t "\$TARGET" \
              -c zap-baseline.conf \
              -J zap.json \
              -r zap.html \
              -w zap.md \
              -m 5 \
              -z "-config api.key=" || true

          # Fail build on High alerts
          if grep -q "High" ${REPORT_DIR}/zap.md; then
            echo "ZAP found High risk alerts. Failing."
            exit 1
          fi
        """
      }
      post {
        always {
          archiveArtifacts artifacts: "${REPORT_DIR}/zap.*", allowEmptyArchive: true
          publishHTML(target: [
            reportDir: "${REPORT_DIR}",
            reportFiles: 'zap.html',
            reportName: 'OWASP ZAP Baseline',
            keepAll: true,
            allowMissing: true
          ])
        }
      }
    }

    stage('Swap Staging -> Production') {
      when { allOf { expression { return params.RUN_DAST }; expression { currentBuild.currentResult == 'SUCCESS' } } }
      steps {
        sh """
          az functionapp deployment slot swap \
            --resource-group "${AZ_RESOURCE_GROUP}" \
            --name "${AZ_FUNCTIONAPP}" \
            --slot "${AZ_SLOT}" \
            --target-slot production
        """
      }
    }
  } // stages

  post {
    always {
      sh 'echo "Build finished with status: ${currentBuild.currentResult}"'
    }
  }
}
