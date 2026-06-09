import json
import time
import urllib.parse
import re
import requests
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import F
from .models import ScannedHistory, SiteStats

# Persistent Session for TCP Keep-Alive connection reuse
session = requests.Session()

def landing_view(request):
    """Renders the SaaS landing page at root URL."""
    stats = SiteStats.get_stats()
    stats.total_visitors = F('total_visitors') + 1
    stats.save()
    stats.refresh_from_db()
    display_visitors = stats.total_visitors
    display_requests = stats.total_requests_made
    
    return render(request, 'landing.html', {
        'display_visitors': display_visitors,
        'display_requests': display_requests
    })


def about_view(request):
    """Renders the About the Developer page."""
    return render(request, 'about.html')


def dashboard_view(request):
    """
    Renders the main bento dashboard page, loading all previously scanned Swagger urls.
    Filtered by the user's current session key to isolate data.
    """
    session_key = request.session.session_key
    if not session_key:
        history = ScannedHistory.objects.none()
    else:
        history = ScannedHistory.objects.filter(session_key=session_key).order_by('-scanned_at')
        
    return render(request, 'dashboard.html', {'history': history})

def resolve_refs(node, root_schema, resolved=None, memo=None):
    """
    Recursively resolves JSON references ($ref) in the Swagger/OpenAPI schema with memoization.
    Handles circular references gracefully and eliminates duplicate schema traversals.
    """
    if resolved is None:
        resolved = set()
    if memo is None:
        memo = {}
    
    if isinstance(node, dict):
        if '$ref' in node:
            ref_path = node['$ref']
            
            # Return cached resolution if available
            if ref_path in memo:
                return memo[ref_path]
                
            if ref_path in resolved:
                return {"type": "object", "description": f"Circular ref to {ref_path}"}
            
            parts = ref_path.lstrip('#/').split('/')
            target = root_schema
            try:
                for part in parts:
                    target = target[part]
                new_resolved = resolved | {ref_path}
                resolved_val = resolve_refs(target, root_schema, new_resolved, memo)
                # Cache resolution results
                memo[ref_path] = resolved_val
                return resolved_val
            except (KeyError, TypeError):
                return {"type": "object", "description": f"Unresolved ref {ref_path}"}
        else:
            return {k: resolve_refs(v, root_schema, resolved, memo) for k, v in node.items()}
    elif isinstance(node, list):
        return [resolve_refs(item, root_schema, resolved, memo) for item in node]
    return node

def resolve_schema_from_html(resp, base_url):
    """
    Attempts to detect if the HTML response is a login page or a Swagger/Redoc UI page,
    and extracts the actual raw OpenAPI spec JSON/YAML URL.
    """
    html_content = resp.text
    final_url = resp.url

    # 1. Check if the final redirected URL points to a login/admin page
    if any(term in final_url.lower() for term in ['/login', '/signin', '/admin/login']):
        raise ValueError("The request redirected to a login or admin page. This endpoint requires authentication.")

    # 2. Check if the HTML title suggests a login/authentication page
    title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
    if title_match:
        title_text = title_match.group(1).lower()
        if any(term in title_text for term in ['log in', 'login', 'sign in', 'signin', 'authorize', 'authentication']):
            raise ValueError("The URL returned a login page. This endpoint requires authentication.")

    # 3. Check for typical password inputs
    if 'name="password"' in html_content or 'type="password"' in html_content:
        raise ValueError("The URL returned a login page with password prompts. This endpoint requires authentication.")

    # 4. Search for common Swagger UI or Redoc configuration patterns (e.g. url: "...")
    patterns = [
        r'url\s*:\s*[\'"]([^\'\"]+)[\'"]',
        r'spec-url\s*=\s*[\'"]([^\'\"]+)[\'"]',
        r'data-url\s*=\s*[\'"]([^\'\"]+)[\'"]',
        r'SwaggerUIBundle\(\s*\{\s*url\s*:\s*[\'"]([^\'\"]+)[\'"]',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            extracted_url = match.group(1)
            # Exclude asset paths
            if not extracted_url.endswith(('.js', '.css', '.png', '.jpg', '.gif', '.ico')):
                return urllib.parse.urljoin(base_url, extracted_url)

    raise ValueError(
        "The URL returned an HTML page instead of a raw OpenAPI JSON/YAML schema. "
        "If this is a Swagger UI page, we could not auto-detect the schema URL."
    )


@csrf_exempt
def parse_swagger(request):
    """
    POST endpoint: fetches a Swagger URL, parses/normalizes it,
    records it in ScannedHistory, and returns the schema tree.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    swagger_url = None
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            swagger_url = data.get('swagger_url')
        except json.JSONDecodeError:
            pass
    else:
        swagger_url = request.POST.get('swagger_url')
        
    if not swagger_url:
        return JsonResponse({'error': 'Missing swagger_url parameter'}, status=400)
        
    # Standardize URL string
    swagger_url = swagger_url.strip()
    
    try:
        resp = session.get(swagger_url, timeout=15)
        resp.raise_for_status()
        
        content_type = resp.headers.get('content-type', '').lower()
        # If the response appears to be HTML, try to resolve the raw schema URL from it
        if 'text/html' in content_type or resp.text.strip().startswith('<!DOCTYPE') or resp.text.strip().startswith('<html'):
            try:
                real_schema_url = resolve_schema_from_html(resp, swagger_url)
                resp = session.get(real_schema_url, timeout=15)
                resp.raise_for_status()
            except ValueError as val_err:
                return JsonResponse({'error': f'Failed to fetch or parse Swagger URL: {str(val_err)}'}, status=400)
            except Exception as fetch_err:
                return JsonResponse({'error': f'Detected Swagger page but failed to fetch raw spec from {real_schema_url}: {str(fetch_err)}'}, status=400)

        try:
            schema = resp.json()
        except Exception as json_err:
            try:
                import yaml
                schema = yaml.safe_load(resp.text)
                if not isinstance(schema, dict):
                    raise ValueError("Parsed content is not a dictionary")
            except Exception as yaml_err:
                raise ValueError(f"Invalid JSON/YAML schema. JSON error: {str(json_err)}. YAML error: {str(yaml_err)}")
    except Exception as e:
        return JsonResponse({'error': f'Failed to fetch or parse Swagger URL: {str(e)}'}, status=400)
        
    # Extract API Title from Swagger/OpenAPI info block
    title = schema.get('info', {}).get('title', 'API Spec').strip()
    if not title:
        title = "Unnamed API"
        
    # Auto-create session if it doesn't exist
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key
        
    # Save/Update history in SQLite isolated by session
    ScannedHistory.objects.update_or_create(
        url=swagger_url,
        session_key=session_key,
        defaults={'title': title}
    )
    
    # Resolve all relative reference links
    try:
        resolved_schema = resolve_refs(schema, schema)
    except Exception as e:
        resolved_schema = schema
        
    # Auto-detect base server URL
    parsed_swagger_url = urllib.parse.urlparse(swagger_url)
    base_url = None
    
    # Check OpenAPI 3.x server configuration
    if 'servers' in resolved_schema and resolved_schema['servers']:
        for server in resolved_schema['servers']:
            url_str = server.get('url', '')
            if url_str:
                if url_str.startswith('http://') or url_str.startswith('https://'):
                    base_url = url_str
                else:
                    base_url = urllib.parse.urljoin(swagger_url, url_str)
                break
    
    # Check Swagger 2.0 host and basePath configuration
    elif 'host' in resolved_schema:
        schemes = resolved_schema.get('schemes', ['https'])
        scheme = schemes[0] if isinstance(schemes, list) and schemes else 'https'
        host = resolved_schema['host']
        base_path = resolved_schema.get('basePath', '')
        base_url = f"{scheme}://{host}{base_path}"
        
    # If no server URL was parsed, resolve to hostname of Swagger config url
    if not base_url:
        base_url = f"{parsed_swagger_url.scheme}://{parsed_swagger_url.netloc}"
        if 'basePath' in resolved_schema:
            base_url = urllib.parse.urljoin(base_url, resolved_schema['basePath'])
            
    endpoints_list = []
    paths = resolved_schema.get('paths', {})
    
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        
        # Paths can have global parameters
        path_parameters = path_item.get('parameters', [])
        
        for method in ['get', 'post', 'put', 'delete', 'patch', 'options', 'head']:
            if method not in path_item:
                continue
            
            op = path_item[method]
            if not isinstance(op, dict):
                continue
                
            op_parameters = op.get('parameters', [])
            
            # Build unified parameters mapping, overriding path levels
            params_dict = {}
            for p in path_parameters + op_parameters:
                if isinstance(p, dict):
                    params_dict[(p.get('name'), p.get('in'))] = p
            
            parameters_list = list(params_dict.values())
            
            # Resolve request body schemas (OpenAPI 3.x vs Swagger 2.0 body parameters)
            request_body_info = None
            if 'requestBody' in op:
                rb = op['requestBody']
                if isinstance(rb, dict):
                    required = rb.get('required', False)
                    content = rb.get('content', {})
                    media_type = 'application/json'
                    schema_details = {}
                    
                    # Inspect media types
                    if content:
                        if 'application/json' in content:
                            media_type = 'application/json'
                            schema_details = content['application/json'].get('schema', {})
                        elif 'application/x-www-form-urlencoded' in content:
                            media_type = 'application/x-www-form-urlencoded'
                            schema_details = content['application/x-www-form-urlencoded'].get('schema', {})
                        elif 'multipart/form-data' in content:
                            media_type = 'multipart/form-data'
                            schema_details = content['multipart/form-data'].get('schema', {})
                        else:
                            media_type = list(content.keys())[0]
                            schema_details = content[media_type].get('schema', {})
                            
                    request_body_info = {
                        'required': required,
                        'mediaType': media_type,
                        'schema': schema_details
                    }
            else:
                # Check for Swagger 2.0 format body parameters
                body_param = next((p for p in parameters_list if p.get('in') == 'body'), None)
                if body_param:
                    parameters_list = [p for p in parameters_list if p.get('in') != 'body']
                    request_body_info = {
                        'required': body_param.get('required', False),
                        'mediaType': 'application/json',
                        'schema': body_param.get('schema', {})
                    }
                    
            # Simplify and structure parameters for frontend generator
            formatted_params = []
            for p in parameters_list:
                formatted_params.append({
                    'name': p.get('name'),
                    'in': p.get('in'),
                    'required': p.get('required', False),
                    'type': p.get('type') or p.get('schema', {}).get('type', 'string'),
                    'description': p.get('description', ''),
                    'default': p.get('default') or p.get('schema', {}).get('default'),
                    'schema': p.get('schema', {})
                })
                
            endpoints_list.append({
                'path': path,
                'method': method.upper(),
                'summary': op.get('summary') or op.get('operationId') or f"{method.upper()} {path}",
                'description': op.get('description', ''),
                'parameters': formatted_params,
                'requestBody': request_body_info,
                'tags': op.get('tags', ['default'])
            })
            
    return JsonResponse({
        'title': title,
        'description': resolved_schema.get('info', {}).get('description', ''),
        'version': resolved_schema.get('info', {}).get('version', ''),
        'baseUrl': base_url,
        'endpoints': endpoints_list
    })

@csrf_exempt
def proxy_request(request):
    """
    POST endpoint: Acts as a backend proxy to execute the client request
    from the dashboard, bypassing CORS, and returning status, latency, headers, and body.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)
    
    target_url = data.get('url')
    method = data.get('method', 'GET').upper()
    headers = data.get('headers', {})
    payload = data.get('body')
    
    if not target_url:
        return JsonResponse({'error': 'Missing url parameter'}, status=400)
        
    start_time = time.perf_counter()
    try:
        # Standardize requests arguments
        req_kwargs = {
            'headers': headers,
            'timeout': 20,
            'verify': False, # Avoid SSL cert verification issues during local/mock API simulation
        }
        
        # Load body payload depending on header type
        if payload is not None:
            if isinstance(payload, (dict, list)):
                req_kwargs['json'] = payload
            elif isinstance(payload, str):
                content_type = headers.get('Content-Type', '').lower()
                if 'application/json' in content_type:
                    try:
                        req_kwargs['json'] = json.loads(payload)
                    except ValueError:
                        req_kwargs['data'] = payload
                else:
                    req_kwargs['data'] = payload
            else:
                req_kwargs['data'] = payload
                
        # Fire proxy request using keep-alive session
        response = session.request(method, target_url, **req_kwargs)
        elapsed_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        stats = SiteStats.get_stats()
        stats.total_requests_made = F('total_requests_made') + 1
        stats.save()
        
        # Try to parse response body as JSON
        is_json = False
        try:
            response_json = response.json()
            is_json = True
        except ValueError:
            response_json = None
            
        return JsonResponse({
            'status_code': response.status_code,
            'status_text': response.reason,
            'elapsed_time_ms': elapsed_time_ms,
            'headers': dict(response.headers),
            'body': response_json if is_json else response.text,
            'is_json': is_json
        })
    except requests.exceptions.RequestException as e:
        elapsed_time_ms = int((time.perf_counter() - start_time) * 1000)
        return JsonResponse({
            'error': str(e),
            'elapsed_time_ms': elapsed_time_ms
        }, status=502)
