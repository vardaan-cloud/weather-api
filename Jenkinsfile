pipeline {
  agent any

  options {
    timestamps()
    ansiColor('xterm')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    durabilityHint('PERFORMANCE_OPTIMIZED')
  }

  parameters {
    string(name: 'PY_VERSION',        defaultValue: '3.11',          description: 'Python version')
    string(name: 'AZ_SUBSCRIPTION',   defaultValue: '',              description: 'Azure Subscription ID')
    string(name: 'AZ_RESOURCE_GROUP', defaultValue: 'rg-free-auto',  description: 'Resource Group')
    string(name: 'AZ_FUNCTIONAPP',    defaultValue: 'vardaan-weather-api', description: 'Function App name')
    string(name: 'AZ_SLOT',           defaultValue: 'staging',       description: 'Deployment slot')
    string(name: 'WARMUP_PATH',       defaultValue: '/',             description: 'Path for warmup')
  }

  environment {
    AZURE_SP_JSON = credentials('AZURE_SP_JSON')
    VENV = '.venv'
    REPORT_DIR = 'build-reports'
    BASE_URL = "https://${AZ_FUNCTIONAPP}-${AZ_SLOT}.azurewebsites.net"
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
        sh '''
          PYBIN=$(command -v python3)
          echo "Using PYBIN=$PYBIN"
          $PYBIN -m venv .venv
          . .venv/bin/activate
          python -V
          pip install --upgrade pip wheel
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
        '''
      }
    }

    stage('Build ZIP') {
      steps {
        sh '''
          mkdir -p dist
          ZIP=dist/functionapp.zip
          rm -f "$ZIP"

          zip -r "$ZIP" . \
            -x "*.git*" ".venv/*" "dist/*" "build-reports/*" "local.settings.json" "tests/*" "Jenkinsfile"

          echo "Built ZIP: $ZIP"
        '''
      }
      post {
        success { archiveArtifacts artifacts: 'dist/functionapp.zip', fingerprint: true }
      }
    }

    stage('Azure Login') {
      steps {
        sh '''
          echo "$AZURE_SP_JSON" > sp.json

          CLIENT_ID=$(jq -r .clientId sp.json)
          CLIENT_SECRET=$(jq -r .clientSecret sp.json)
          TENANT_ID=$(jq -r .tenantId sp.json)

          az login --service-principal -u "$CLIENT_ID" -p "$CLIENT_SECRET" --tenant "$TENANT_ID"
          az account set --subscription "$AZ_SUBSCRIPTION"
          az account show
        '''
      }
    }

    stage('Deploy to Staging Slot') {
      steps {
        sh '''
          SLOT_EXISTS=$(az functionapp deployment slot list -g "$AZ_RESOURCE_GROUP" -n "$AZ_FUNCTIONAPP" | jq -r --arg SLOT "$AZ_SLOT" '.[] | select(.name==$SLOT) | .name')

          if [ -z "$SLOT_EXISTS" ]; then
            echo "Creating slot $AZ_SLOT..."
            az functionapp deployment slot create -g "$AZ_RESOURCE_GROUP" -n "$AZ_FUNCTIONAPP" --slot "$AZ_SLOT"
          fi

          az functionapp deployment source config-zip \
            --resource-group "$AZ_RESOURCE_GROUP" \
            --name "$AZ_FUNCTIONAPP" \
            --slot "$AZ_SLOT" \
            --src dist/functionapp.zip
        '''
      }
    }

    stage('Warmup') {
      steps {
        sh '''
          URL="${BASE_URL}${WARMUP_PATH}"
          echo "Warming up URL: $URL"

          for i in 1 2 3 4 5; do
            curl -sSf "$URL" && break || sleep 5
          done || true

          echo "Warmup complete."
        '''
      }
    }
  }

  post {
    always {
      echo "Build finished with status: ${currentBuild.currentResult}"
    }
  }
}
