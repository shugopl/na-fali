"""Structural guarantees in generated deployment; schema validation is a separate gate."""
import importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('configuration',ROOT/'scripts/configure-k3s.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class DeploymentTests(unittest.TestCase):
    def config(self):
        return {'namespace':'na-fali','host':'kurs.example.org','image':'ghcr.io/example/na-fali:3.0.0','admin':'admin','email':'admin@example.org','storage_class':'local-path','storage_size':'5Gi','tls_secret':'na-fali-tls','replicas':2,'ingress_class':'traefik','ingress_namespace':'kube-system','ingress_name':'traefik'}
    def test_secrets_do_not_reach_application(self):
        docs=m.render(self.config(),'bootstrap-password','app-db-password','django-key','db-admin-password')
        app=next(x for x in docs['application.json'] if x['kind']=='Deployment')
        refs=app['spec']['template']['spec']['containers'][0]['envFrom']
        self.assertNotIn({'secretRef':{'name':'na-fali-bootstrap'}},refs)
        self.assertNotIn({'secretRef':{'name':'na-fali-db-admin'}},refs)
        self.assertIn({'secretRef':{'name':'na-fali-db'}},refs)
    def test_admin_reset_is_opt_in_and_only_in_job(self):
        cfg=self.config()
        normal=m.render(cfg,'a','b','c')
        cfg['reset_admin_password']=True
        reset=m.render(cfg,'a','b','c')
        normalcmd=normal['bootstrap.json'][0]['spec']['template']['spec']['containers'][0]['command'][-1]
        resetcmd=reset['bootstrap.json'][0]['spec']['template']['spec']['containers'][0]['command'][-1]
        self.assertNotIn('--reset-admin-password',normalcmd)
        self.assertIn('--reset-admin-password',resetcmd)
    def test_tls_persistent_db_and_scoped_network(self):
        docs=m.render(self.config(),'a','b','c')
        ingress=next(x for x in docs['application.json'] if x['kind']=='Ingress')
        self.assertEqual(ingress['spec']['tls'][0]['secretName'],'na-fali-tls')
        db=next(x for x in docs['infrastructure.json'] if x['kind']=='StatefulSet')
        self.assertEqual(db['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'],'local-path')
        policies=[x for doc in docs.values() for x in doc if x['kind']=='NetworkPolicy']
        self.assertEqual(len(policies),2)
