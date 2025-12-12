pipeline {
  agent any
  options { timestamps(); ansiColor('xterm') }

  parameters {
    string(name: 'AZ_SUBSCRIPTION',   defaultValue: '',              description: 'Azure Subscription ID')
    string(name: 'AZ_RESOURCE_GROUP', defaultValue: 'rg-free-auto',  description: 'Azure Resource Group')
    string(name: 'AZ_FUNCTIONAPP',    defaultValue: 'vardaan-weather-api', description: 'Function App name')
    string(name: 'AZ_SLOT',           defaultValue: 'staging',       description: 'Deployment slot')
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
          python -V
          python -m pip install --upgrade pip setuptools wheel
          pip install --no-cache-dir -r requirements.txt || (sleep 3 && pip install --no-cache-dir -r requirements.txt)
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
      post { success { archiveArtifacts artifacts: 'dist/functionapp.zip', fingerprint: true } }
    }

    stage('Azure Login') {
      steps {
        sh '''
          echo "$AZURE_SP_JSON" > sp.json
          CLIENT_ID=$(jq -r .clientId sp.json)
          CLIENT_SECRET=$(jq -r .clientSecret sp.json)
          TENANT_ID=$(jq -r .tenantId sp.json)

          az config set extension.use_dynamic_install=yes_without_prompt
          az login --service-principal -u "$CLIENT_ID" -p "$CLIENT_SECRET" --tenant "$TENANT_ID"
          az account set --subscription "$AZ_SUBSCRIPTION"
          az account show
        '''
      }
    }

    stage('Ensure Azure Resources') {
      steps {
        sh '''
          set -e
          RG="$AZ_RESOURCE_GROUP"; APP="$AZ_FUNCTIONAPP"; SLOT="$AZ_SLOT"
          LOCATION=$(az group show -n "$RG" --query location -o tsv)
          if [ -z "$LOCATION" ]; then
            echo "Resource group $RG not found."; exit 3
          fi
          echo "Using location: $LOCATION"

          BASE=$(echo "$APP" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')
          CAND="${BASE}sa"; CAND=$(echo "$CAND" | cut -c1-22)$(shuf -i 10-99 -n 1)
          SA_NAME="$CAND"

          EXISTING_SA=$(az storage account list -g "$RG" --query "[?tags.app=='$APP'].name | [0]" -o tsv || true)
          if [ -n "$EXISTING_SA" ]; then
            SA_NAME="$EXISTING_SA"; echo "Reusing storage: $SA_NAME"
          else
            echo "Creating storage: $SA_NAME"
            az storage account create -n "$SA_NAME" -g "$RG" -l "$LOCATION" --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false
            az tag create --resource-id $(az storage account show -n "$SA_NAME" -g "$RG" --query id -o tsv) --tags app="$APP" || true
          fi

          if ! az functionapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
            echo "Creating Function App: $APP"
            az functionapp create -g "$RG" -n "$APP" --storage-account "$SA_NAME" \
              --consumption-plan-location "$LOCATION" --runtime python --runtime-version 3.11 \
              --functions-version 4 --os-type linux
          else
            echo "Function App exists: $APP"
          fi

          if ! az functionapp deployment slot list -g "$RG" -n "$APP" | jq -e --arg s "$SLOT" '.[] | select(.name==$s)' >/dev/null; then
            echo "Creating slot: $SLOT"
            az functionapp deployment slot create -g "$RG" -n "$APP" --slot "$SLOT"
          else
            echo "Slot exists: $SLOT"
          fi
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
          URL="https://'${AZ_FUNCTIONAPP}'-'${AZ_SLOT}'.azurewebsites.net/api/health"
          echo "Warming: $URL"
          for i in 1 2 3 4 5; do
            curl -sSf "$URL" && break || sleep 5
          done || true
          echo "Visit: $URL"
        '''
      }
    }
  }

  post { always { echo "Build completed with status: ${currentBuild.currentResult}" } }
}
