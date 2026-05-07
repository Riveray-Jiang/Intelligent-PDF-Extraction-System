# PDF Extraction Frontend

React + TypeScript + Vite UI for the PDF extraction product server.

## Run Locally
Start the backend first from the repository root:
```powershell
.\scripts\run_product_server.ps1
```

Then start the frontend:
```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open `http://127.0.0.1:5173/`.

## Checks
```powershell
npm run lint
npm run build
```

The app defaults to `http://127.0.0.1:8892` for the backend. To override it, create `frontend/.env` from `frontend/.env.example`.
