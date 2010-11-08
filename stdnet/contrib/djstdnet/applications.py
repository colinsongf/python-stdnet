from djpcms.views import appsite, appview

from stdnet.contrib.djstdnet import views


class StdnetMonitorApplication(appsite.ApplicationBase):
    name = 'Stdnet Monitor'
    list_per_page = 100
    
    home  = views.RedisHomeView(isplugin = True, isapp = True)
    db    = views.RedisDbView(regex = '(?P<db>\d+)', isapp = True)
    flush = views.RedisDbFlushView(regex = 'flush', parent = 'db')
    
    def dburl(self, db):
        dbview = self.getview('db')
        djp = view(request, db = db)
        return djp.url
    
    
class StdModelApplication(appsite.ModelApplication):
    search      = appview.SearchView()
    information = views.StdModelInformationView(regex = 'info')
    add         = appview.AddView()
    view        = appview.ViewView()
    edit        = appview.EditView(isplugin = True)