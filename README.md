



# SMG Cloud Provisioner

A cloud-agnostic Kubernetes node auto-scaler that dynamically provisions GPU instances based on workload demand. Built for hybrid cloud environments with secure networking integration.

## Architecture Overview

### Core Components

**🏗️ Cloud Infrastructure Layer**
- `gce.py` - Google Cloud Compute Engine instance management
- `k8s.py` - Kubernetes cluster integration and node lifecycle management
- `tailscale_provider.py` - Secure networking and VPN key management

**⚡ Orchestration Layer**
- `provisioner.py` - Auto-scaling logic and resource management
- `main.py` - FastAPI web server and system orchestration
- `async_tools.py` - Asynchronous utilities and threading helpers

### System Flow

```
1. 📊 Monitor K8s GPU pod pressure
2. 🚀 Trigger instance provisioning when demand exceeds capacity
3. 🔑 Generate fresh Tailscale auth keys for secure networking
4. ☁️  Create GCE instances from templates with dynamic startup scripts
5. 🔗 Join new nodes to K3s cluster automatically
6. 📉 Scale down when demand decreases
```

### Key Features

- **Cloud Agnostic Design** - Modular architecture supports multiple cloud providers
- **Dynamic Configuration** - Startup scripts with automatic variable substitution (`__VAR_NAME__`)
- **Secure Networking** - Integrated Tailscale mesh networking with auto-rotating keys
- **Async Operations** - Non-blocking instance creation and deletion
- **Environment-Driven** - 12-factor app compliance with comprehensive ENV configuration
- **GPU-Aware Scaling** - Intelligent scaling based on GPU workload demand

### Configuration

All configuration is environment-driven. See [ENV.md](ENV.md) for complete variable documentation.

---

## AI Disclosure 
AI was used to assist in the creation of this code. The model did not design the architecture 
and is responsible for less than 5% of the code. All code was reviewed and tested by me. 

- Assisted in writing less than 5% of the code including some helper functions.
- Generating some of the docs and collecting env vars/cataloging them.
- Reviewing code for potential bugs and improvements. Improvements were implemented by human.
- A rubber duck to go over my ideas with and get feed back on my approach and understanding.
- Generated most things above this Disclosure, I like writing docs, but I mean genAI is badass. 