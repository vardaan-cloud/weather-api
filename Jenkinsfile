pipeline {
  agent any

  options {
    timestamps()
    ansiColor('xterm')
  }

  parameters {
    string(name: 'AZ_SUBSCRIPTION', defaultValue: '', description: 'Azure Subscription ID')
    string(name: 'AZ_RESOURCE_GROUP', defaultValue: 'rg-free-auto', description: 'Azure Resource Group')
    string(name: 'AZ_FUNCTIONAPP', defaultValue: 'vardaan-weather-api', description: 'Function App Name')
    string(name: 'AZ_SLOT', defaultValue: 'staging', description: 'Deployment Slot')
  }

  environment {
    AZURE_SP_JSON = credentials('AZURE_SP_JSON')
  }

  stages {

    stage('Checkout') {
      steps {
        deleteDir()
        checkout scm
      }
    }

    stage('Setup Python') {
      steps {
        sh '''
          python3 -m venv .venv
          . .venv/bin/activate
          pip install --upgrade pip wheel
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
        '''
      }
    }

    stage('Build ZIP') {
      steps {
        sh '''
          mkdir -p dist
          ZIP="dist/functionapp.zip"
          rm -f "$ZIP"

          zip -r "$ZIP" . \
            -x ".git/*" ".venv/*" "dist/*" "local.settings.json" "Jenkinsfile" "tests/*"

          echo "ZIP built: $ZIP"
        '''
      }
      post {
        success {
          archiveArtifacts artifacts: 'dist/functionapp.zip'
        }
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
        '''
      }
    }

    stage('Deploy to Staging') {
      steps {
        sh '''
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
          URL="https://'${AZ_FUNCTIONAPP}'-'${AZ_SLOT}'.azurewebsites.net/"
          echo "Warming: $URL"
          curl -sS $URL || true
        '''
      }
    }
  }

  post {
    always {
      echo "Build completed with status: ${currentBuild.currentResult}"
    }
  }

}
