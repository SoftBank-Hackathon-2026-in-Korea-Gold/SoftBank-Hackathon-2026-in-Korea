import rumps


class App(rumps.App):
    @rumps.clicked("Hello")
    def hello(self, _):
        rumps.alert("hi")


App("Menubar").run()
